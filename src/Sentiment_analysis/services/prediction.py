"""Predict comment sentiment without fitting a vocabulary or classifier."""

import json
import math
import subprocess
import sys
from collections import Counter
from importlib.metadata import version

import joblib
import numpy as np
from threadpoolctl import threadpool_limits

from src.Sentiment_analysis.entity.entity import ModelPredictionConfig
from src.Sentiment_analysis.utils.common import sha256_file
from src.Sentiment_analysis.utils.text import normalize_text, validate_text_preprocessing


class PredictionService:
    """Load one verified model and reuse it for successive prediction requests."""

    def __init__(self, config: ModelPredictionConfig):
        self.config = config
        params = config.parameters
        self.batch_size = self._positive_integer(params["batch_size"], "batch_size")
        self.max_comments = self._positive_integer(params["max_comments"], "max_comments")
        self.threshold = float(params["review_confidence_threshold"])
        if not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("review_confidence_threshold must be between zero and one")
        self._load_model()

    @staticmethod
    def _positive_integer(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value

    def _load_model(self):
        config = self.config
        if not config.model_file.is_file() or not config.training_report.is_file():
            if not config.restore_with_dvc:
                raise FileNotFoundError("Trained model/report is missing; run training or pull its DVC artifacts")
            subprocess.run([sys.executable, "-m", "dvc", "pull", "artifacts/model_training"],
                           cwd=config.project_root, check=True)
        report = json.loads(config.training_report.read_text(encoding="utf-8"))
        if report.get("passed") is not True:
            raise ValueError("A successful training report is required for prediction")
        # DVC restores artifacts produced by this project. Verify before deserializing.
        if sha256_file(config.model_file) != report["model_sha256"]:
            raise ValueError("Model checksum changed after training")
        settings = report.get("text_preprocessing")
        validate_text_preprocessing(settings)
        if {int(key): value for key, value in report["label_mapping"].items()} != config.label_mapping:
            raise ValueError("Training label mapping differs from prediction configuration")
        for package, recorded in report["package_versions"].items():
            if version(package) != recorded:
                raise ValueError(f"Install the trained model's {package}=={recorded} before prediction")
        bundle = joblib.load(config.model_file)
        if (bundle.get("text_preprocessing") != settings
                or bundle.get("preprocessing_sha256") != report["preprocessing_sha256"]
                or bundle.get("package_versions") != report["package_versions"]
                or bundle.get("label_mapping") != config.label_mapping
                or bundle.get("text_column") != config.text_column):
            raise ValueError("Model bundle and training metadata do not match prediction configuration")
        pipeline = bundle["pipeline"]
        if list(pipeline.named_steps) != ["tfidf", "classifier"]:
            raise ValueError("Prediction requires the trained TF-IDF + logistic regression pipeline")
        self.classes = pipeline.named_steps["classifier"].classes_.tolist()
        if self.classes != sorted(config.label_mapping):
            raise ValueError("Model classes differ from the configured labels")
        self.pipeline = pipeline
        self.text_settings = settings
        self.training = report

    def predict(self, comments):
        """Accept raw strings (or missing values), retaining input order and skips."""
        if not isinstance(comments, list):
            raise TypeError("comments must be a list of strings or null values")
        if len(comments) > self.max_comments:
            raise ValueError(f"At most {self.max_comments} comments are allowed per request")
        if any(text is not None and not isinstance(text, str) for text in comments):
            raise TypeError("Each comment must be a string or null")
        rows = []
        candidates = []
        for index, text in enumerate(comments):
            normalized = normalize_text(text or "", self.text_settings["unicode_normalization"])
            row = {"index": index, "text": text, "normalized_text": normalized,
                   "status": "skipped", "sentiment": None, "sentiment_label": None,
                   "confidence": None, "class_probabilities": None, "needs_review": False,
                   "skipped_reason": "empty_text" if not normalized else "no_known_features"}
            rows.append(row)
            if normalized:
                candidates.append(index)

        vectorizer = self.pipeline.named_steps["tfidf"]
        classifier = self.pipeline.named_steps["classifier"]
        for start in range(0, len(candidates), self.batch_size):
            indices = candidates[start:start + self.batch_size]
            features = vectorizer.transform([rows[index]["normalized_text"] for index in indices])
            known = np.asarray(features.getnnz(axis=1)).ravel() > 0
            if not known.any():
                continue
            with threadpool_limits(limits=int(self.training["parameters"]["thread_limit"])):
                probabilities = classifier.predict_proba(features[known])
            if (not np.isfinite(probabilities).all() or (probabilities < 0).any()
                    or (probabilities > 1).any() or not np.allclose(probabilities.sum(axis=1), 1)):
                raise ValueError("Model returned invalid class probabilities")
            for index, scores in zip(np.asarray(indices)[known].tolist(), probabilities, strict=True):
                label = int(self.classes[int(scores.argmax())])
                confidence = float(scores.max())
                rows[index].update({
                    "status": "analyzed", "sentiment": self.config.label_mapping[label],
                    "sentiment_label": label, "confidence": confidence,
                    "class_probabilities": {self.config.label_mapping[int(key)]: float(score)
                                            for key, score in zip(self.classes, scores, strict=True)},
                    "needs_review": confidence < self.threshold, "skipped_reason": None,
                })
        analyzed = [row for row in rows if row["status"] == "analyzed"]
        skips = Counter(row["skipped_reason"] for row in rows if row["status"] == "skipped")
        counts = Counter(row["sentiment"] for row in analyzed)
        return {
            "schema_version": 1,
            "summary": {"received": len(rows), "analyzed": len(analyzed), "skipped": len(rows) - len(analyzed),
                        "coverage": len(analyzed) / len(rows) if rows else 0.0,
                        "sentiment_counts": {self.config.label_mapping[key]: counts[self.config.label_mapping[key]]
                                             for key in self.classes},
                        "skipped_by_reason": {key: skips[key] for key in ["empty_text", "no_known_features"]},
                        "needs_review": sum(row["needs_review"] for row in analyzed)},
            "model": {"type": self.training["model_type"], "sha256": self.training["model_sha256"],
                      "mlflow_run_id": self.training.get("mlflow", {}).get("run_id"),
                      "text_preprocessing": dict(self.text_settings),
                      "review_confidence_threshold": self.threshold,
                      "confidence_definition": "Highest class score, not calibrated correctness probability"},
            "comments": rows,
        }
