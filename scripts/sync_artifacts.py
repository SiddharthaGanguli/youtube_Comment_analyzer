"""Push each DVC stage, verify S3 content, and back up MLflow's run database."""

import configparser
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.utils.aws_credentials import aws_credentials
from src.Sentiment_analysis.utils.common import read_yaml, sha256_file, write_json
from src.Sentiment_analysis.utils.experiment_tracking import load_mlflow

STAGES = ("ingestion", "validation", "preprocessing", "training", "evaluation")


def verify_object(client, bucket, key, expected_md5, expected_size):
    response = client.get_object(Bucket=bucket, Key=key)
    digest = hashlib.md5(usedforsecurity=False)
    count = 0
    with response["Body"] as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
            count += len(chunk)
    if count != expected_size or digest.hexdigest() != expected_md5:
        raise ValueError(f"Remote checksum/size mismatch: s3://{bucket}/{key}")
    return {"s3_uri": f"s3://{bucket}/{key}", "size_bytes": count,
            "md5": digest.hexdigest(), "verified": True}


def main():
    manager = ConfigurationManager()
    settings = manager.get_mlflow_config()
    if settings is None:
        raise SystemExit("Enable MLflow before synchronizing experiment artifacts")
    training_path = PROJECT_ROOT / manager.config["model_training"]["report_file"]
    evaluation_path = PROJECT_ROOT / manager.config["model_evaluation"]["report_file"]
    training = json.loads(training_path.read_text(encoding="utf-8"))
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    if not training.get("passed") or not evaluation.get("passed") or not training.get("mlflow"):
        raise SystemExit("Successful MLflow training and evaluation are required before sync")
    if evaluation.get("mlflow", {}).get("run_id") != training["mlflow"]["run_id"]:
        raise SystemExit("Training and evaluation MLflow run IDs differ")
    status = subprocess.run([sys.executable, "-m", "dvc", "status", "--json"], cwd=PROJECT_ROOT,
                            check=True, capture_output=True, text=True)
    if json.loads(status.stdout):
        raise SystemExit("Run python -m dvc repro before synchronizing changed pipeline artifacts")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PROJECT_ROOT / ".dvc/config", encoding="utf-8")
    remote = parser['\'remote "storage"\'']
    location = urlparse(remote["url"])
    if location.scheme != "s3":
        raise ValueError("Artifact verification currently requires an S3 DVC remote")
    bucket, prefix = location.netloc, location.path.strip("/")
    cache = PROJECT_ROOT / ".dvc/cache/files/md5"
    lock = read_yaml(PROJECT_ROOT / "dvc.lock")
    lock_sha = sha256_file(PROJECT_ROOT / "dvc.lock")
    manifest = {"verified_at": datetime.now(timezone.utc).isoformat(), "dvc_lock_sha256": lock_sha,
                "remote": remote["url"], "stages": {}}

    with aws_credentials(PROJECT_ROOT):
        client = boto3.client("s3", region_name=remote.get("region", "us-east-1"))
        mlflow = load_mlflow()
        mlflow.set_tracking_uri(settings.tracking_uri)
        run = mlflow.get_run(training["mlflow"]["run_id"])
        if run.info.status != "FINISHED" or run.data.metrics.get("evaluation_passed") != 1:
            raise ValueError("The MLflow run must finish successfully with test evaluation")
        verified = {}
        for stage in STAGES:
            print(f"Pushing and verifying {stage}", flush=True)
            subprocess.run([sys.executable, "-m", "dvc", "push", stage, "--jobs", "4"],
                           cwd=PROJECT_ROOT, check=True)
            outputs = []
            for output in lock["stages"][stage]["outs"]:
                # Uncached metrics are stored by MLflow and versioned with Git.
                if "md5" not in output or output["path"] == manager.config["model_evaluation"]["metrics_file"]:
                    continue
                checksum = output["md5"]
                object_file = cache / checksum[:2] / checksum[2:]
                key = f"{prefix}/files/md5/{checksum[:2]}/{checksum[2:]}"
                if key not in verified:
                    verified[key] = verify_object(client, bucket, key, checksum.removesuffix(".dir"), object_file.stat().st_size)
                item = {"path": output["path"], "object": verified[key], "files": []}
                if checksum.endswith(".dir"):
                    for entry in json.loads(object_file.read_text(encoding="utf-8")):
                        digest = entry["md5"]
                        entry_file = cache / digest[:2] / digest[2:]
                        entry_key = f"{prefix}/files/md5/{digest[:2]}/{digest[2:]}"
                        if entry_key not in verified:
                            verified[entry_key] = verify_object(client, bucket, entry_key, digest, entry_file.stat().st_size)
                        item["files"].append({"path": f"{output['path']}/{entry['relpath']}", **verified[entry_key]})
                outputs.append(item)
            manifest["stages"][stage] = outputs

        # Download the logged model and check inference, rather than only checking object existence.
        model = mlflow.sklearn.load_model(training["mlflow"]["model_uri"])
        import joblib
        import numpy as np
        local = joblib.load(PROJECT_ROOT / manager.config["model_training"]["model_file"])["pipeline"]
        examples = ["I enjoyed this video.", "This was not helpful.", "The video was uploaded today."]
        np.testing.assert_allclose(model.predict_proba(examples), local.predict_proba(examples))
        manifest["mlflow"] = {**training["mlflow"], "model_download_and_prediction_verified": True}

        database = PROJECT_ROOT / manager.config["mlflow"]["database"]
        snapshot = PROJECT_ROOT / "artifacts/experiment_tracking/mlflow.db"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as destination:
            source.backup(destination)
        snapshot_key = f"{prefix}/tracking/{run.info.run_id}/{lock_sha}/mlflow.db"
        client.upload_file(str(snapshot), bucket, snapshot_key)
        with snapshot.open("rb") as stream:
            snapshot_md5 = hashlib.file_digest(stream, "md5").hexdigest()
        manifest["tracking_database_backup"] = verify_object(client, bucket, snapshot_key, snapshot_md5, snapshot.stat().st_size)
        manifest["tracking_database_backup"]["sha256"] = sha256_file(snapshot)
        lock_key = f"{prefix}/manifests/{lock_sha}/dvc.lock"
        client.upload_file(str(PROJECT_ROOT / "dvc.lock"), bucket, lock_key)
        manifest["dvc_lock_s3_uri"] = f"s3://{bucket}/{lock_key}"
        manifest_key = f"{prefix}/manifests/{lock_sha}/s3_artifacts_manifest.json"
        manifest["manifest_s3_uri"] = f"s3://{bucket}/{manifest_key}"
        manifest_path = PROJECT_ROOT / "reports/s3_artifacts_manifest.json"
        write_json(manifest_path, manifest)
        client.upload_file(str(manifest_path), bucket, manifest_key)
    print("All five stages and the MLflow model/database backup are verified on S3.")


if __name__ == "__main__":
    main()
