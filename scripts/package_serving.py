"""Build Linux x86_64 Lambda ZIPs from a clean, committed project revision.

No Docker, credentials or AWS calls are needed. Dependencies are resolved for
the deployed Python platform, never copied from the local Windows environment.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def golden_examples(predictions, model_sha256, threshold):
    if (predictions.get("model", {}).get("sha256") != model_sha256
            or predictions["model"].get("review_confidence_threshold") != threshold):
        raise ValueError("Golden predictions must match the packaged model and review threshold")
    rows = predictions.get("comments")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("Provide 1..32 handwritten golden prediction examples")
    return {"model_sha256": model_sha256, "inputs": [row["text"] for row in rows],
            "expected": [{key: row[key] for key in
                ("status", "sentiment", "skipped_reason", "class_probabilities")} for row in rows]}


def write_zip(directory, destination):
    files = []
    for path in directory.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Lambda input must not contain linked files")
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if any(part in {"__pycache__", "tests", "bin"} for part in relative.parts) or path.suffix == ".pyc":
            continue
        files.append((relative.as_posix(), path))
    size = sum(path.stat().st_size for _, path in files)
    if size > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("Uncompressed Lambda package exceeds 250 MiB; use a verified container image")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return {"file": destination.name, "sha256": sha256(destination),
            "bytes": destination.stat().st_size, "uncompressed_bytes": size}


def package_release(output, *, install_dependencies=True):
    from src.Sentiment_analysis.config.serving import ServingConfigurationManager
    from src.Sentiment_analysis.entity.analysis import ModelReference
    from src.Sentiment_analysis.entity.api import release_fingerprint

    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    inputs = ["src", "logging_", "config/serving.yaml", "requirements-lambda-api.txt",
              "requirements-lambda-worker.txt", "requirements-serving-core.txt", "requirements-pipeline.txt"]
    if subprocess.check_output(["git", "status", "--porcelain", "--", *inputs], cwd=ROOT, text=True).strip():
        raise ValueError("Commit backend source and dependency pins before packaging an immutable serving release")
    source_files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", revision, "--",
                                            "src", "logging_", "config/serving.yaml"], cwd=ROOT, text=True).splitlines()
    source_files = [name for name in source_files if name.endswith(".py") or name == "config/serving.yaml"]
    settings = ServingConfigurationManager().get_serving_config()
    if sha256(settings.model.model_file) != settings.model.sha256:
        raise ValueError("Model checksum differs from serving configuration")
    reference = ModelReference(release_id="sr_" + "0" * 64, sha256=settings.model.sha256,
        preprocessing_sha256=settings.model.preprocessing_sha256,
        review_confidence_threshold=settings.worker.review_confidence_threshold,
        result_schema_version=settings.model.result_schema_version)
    release_id = release_fingerprint(reference, revision)
    golden = golden_examples(json.loads((ROOT / "artifacts/model_prediction/predictions.json").read_text(encoding="utf-8")),
                             settings.model.sha256, settings.worker.review_confidence_threshold)
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Package output must stay inside the project's artifacts directory")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty release output directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "code_revision": revision, "release_id": release_id,
                "model_sha256": settings.model.sha256, "packages": {}}
    for kind in ("api", "worker"):
        with tempfile.TemporaryDirectory(prefix=f".lambda-{kind}-", dir=output.parent) as temporary:
            staged = Path(temporary)
            if install_dependencies:
                subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                    "--only-binary=:all:", "--implementation", "cp", "--python-version", "3.12", "--abi", "cp312",
                    "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
                    "--target", str(staged), "-r", str(ROOT / f"requirements-lambda-{kind}.txt")], check=True, cwd=ROOT)
            for name in source_files:
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT))
            if kind == "worker":
                model_directory = staged / "artifacts/model_training"
                model_directory.mkdir(parents=True)
                (model_directory / "sentiment_model.joblib").write_bytes(settings.model.model_file.read_bytes())
                (model_directory / "training_report.json").write_bytes(settings.model.training_report.read_bytes())
                (staged / "config/serving-golden.json").write_text(json.dumps(golden, allow_nan=False), encoding="utf-8")
            manifest["packages"][kind] = write_zip(staged, output / f"{kind}.zip")
    manifest["dependencies_installed"] = install_dependencies
    (output / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Packaged {release_id} in {output}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package_release(args.output)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
