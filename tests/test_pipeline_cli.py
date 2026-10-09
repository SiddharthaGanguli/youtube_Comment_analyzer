import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

from src.Sentiment_analysis.utils.common import sha256_file


class PipelineCliTests(unittest.TestCase):
    def test_all_five_stages_run_from_another_working_directory(self):
        project_root = Path(__file__).resolve().parents[1]
        test_directory = project_root / "artifacts"
        test_directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_directory, prefix="cli-check-") as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(test_directory.resolve()))
            rows = [[f"video {group}", f"{word} video{group} 😊", name, label]
                    for group in range(30)
                    for label, (word, name) in enumerate([("terrible", "Negative"), ("ordinary", "Neutral"),
                                                         ("wonderful", "Positive")])]
            frame = pd.DataFrame(rows, columns=["VideoTitle", "CommentText", "Sentiment", "Sentiment_label"])
            raw = root / "raw.csv"
            frame.to_csv(raw, index=False)
            config = yaml.safe_load((project_root / "config/config.yaml").read_text(encoding="utf-8"))
            config["dataset"]["raw_path"] = str(raw)
            config["dataset"]["dvc_file"] = str(root / "raw.csv.dvc")
            config["dataset"]["source"].update(sha256=sha256_file(raw), size_bytes=raw.stat().st_size)
            sections = ["data_ingestion", "data_validation", "data_preprocessing", "model_training", "model_evaluation"]
            for section in sections:
                for key, value in list(config[section].items()):
                    if key == "root_dir":
                        config[section][key] = str(root / section)
                    elif key.endswith("file") or key.startswith("confusion_matrix_"):
                        config[section][key] = str(root / section / Path(value).name)
            config["data_validation"]["required_columns"] = frame.columns.tolist()
            config["mlflow"]["enabled"] = False
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            params = yaml.safe_load((project_root / "config/params.yaml").read_text(encoding="utf-8"))
            params["model_training"]["tfidf"].update(min_df=1, max_features=1000)
            params["model_training"]["logistic_regression"]["solver"] = "lbfgs"
            params["model_training"]["thread_limit"] = 1
            params_path = root / "params.yaml"
            params_path.write_text(yaml.safe_dump(params, allow_unicode=True), encoding="utf-8")

            result = subprocess.run([sys.executable, str(project_root / "main.py"), "--stage", "all",
                                     "--config", str(config_path), "--params", str(params_path)],
                                    cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            for section in sections:
                report = json.loads(Path(config[section]["report_file"]).read_text())
                self.assertTrue(report["passed"], section)
            metrics = json.loads(Path(config["model_evaluation"]["metrics_file"]).read_text())
            self.assertEqual(metrics["test_rows"], 9)
            self.assertEqual(metrics["evaluation_passed"], 1)
