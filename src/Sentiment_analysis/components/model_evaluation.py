import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix, ConfusionMatrixDisplay

from logging_.logger_ import Logger
from src.Sentiment_analysis.entity.entity import ModelEvaluationConfig
from src.Sentiment_analysis.utils.common import (
    check_split_overlap, classification_metrics, load_verified_split, sha256_file, write_json,
)
from src.Sentiment_analysis.utils.experiment_tracking import experiment_run


class ModelEvaluation:
    def __init__(self, config: ModelEvaluationConfig):
        self.config = config
        self.logger = Logger("model_evaluation.log").get_logger()

    def run(self):
        try:
            training = json.loads(self.config.training_report.read_text(encoding="utf-8"))
            if self.config.mlflow and not training.get("mlflow"):
                raise ValueError("Training must log an MLflow run before tracked evaluation")
            with experiment_run(self.config.mlflow, training.get("mlflow")) as tracking:
                return self._run(tracking)
        except Exception as error:
            write_json(self.config.report_file, {"passed": False, "error": str(error)})
            write_json(self.config.metrics_file, {"evaluation_passed": 0})
            raise

    def _run(self, tracking):
        config = self.config
        report = {"passed": False}
        try:
            training = json.loads(config.training_report.read_text(encoding="utf-8"))
            preprocessing = json.loads(config.preprocessing_report.read_text(encoding="utf-8"))
            if not training.get("passed") or not preprocessing.get("passed"):
                raise ValueError("Successful preprocessing and training are required")
            preprocessing_hash = sha256_file(config.preprocessing_report)
            if preprocessing_hash != training["preprocessing_sha256"]:
                raise ValueError("The preprocessing report changed after training")
            if sha256_file(config.model_file) != training["model_sha256"]:
                raise ValueError("Model checksum changed after training")

            # Check the locally produced model's fingerprint before loading it.
            bundle = joblib.load(config.model_file)
            if bundle["preprocessing_sha256"] != preprocessing_hash or bundle["label_mapping"] != config.label_mapping:
                raise ValueError("The model's dataset or label mapping does not match evaluation")
            if bundle["text_column"] != config.text_column:
                raise ValueError("The model's text column does not match evaluation")
            frames = {}
            for name, path in [("train", config.train_file), ("validation", config.validation_file), ("test", config.test_file)]:
                frames[name] = load_verified_split(
                    path, preprocessing["splits"][name], config.encoding, config.text_column,
                    config.title_column, config.target_column, config.label_mapping,
                )
            check_split_overlap(frames, config.text_column, config.title_column)
            test = frames["test"]
            pipeline = bundle["pipeline"]
            labels = sorted(config.label_mapping)
            if pipeline.named_steps["classifier"].classes_.tolist() != labels:
                raise ValueError("Model classes differ from the configured target labels")
            probabilities = pipeline.predict_proba(test[config.text_column])
            predictions = np.asarray(labels)[probabilities.argmax(axis=1)]
            targets = test[config.target_column]
            test_metrics = classification_metrics(targets, predictions, labels, probabilities)
            baseline = classification_metrics(targets, np.full(len(test), training["majority_label"]), labels)
            flat_metrics = {"evaluation_passed": 1, "test_rows": len(test),
                            **{f"test_{key}": value for key, value in test_metrics.items()},
                            **{f"majority_baseline_{key}": value for key, value in baseline.items()},
                            "accuracy_gain_over_majority": test_metrics["accuracy"] - baseline["accuracy"]}
            names = [config.label_mapping[label] for label in labels]
            per_class = classification_report(targets, predictions, labels=labels, target_names=names,
                                              output_dict=True, zero_division=0)
            matrix = confusion_matrix(targets, predictions, labels=labels)
            write_json(config.classification_report_file, per_class)
            pd.DataFrame(matrix, index=pd.Index(names, name="True label"),
                         columns=pd.Index(names, name="Predicted label")).to_csv(config.confusion_matrix_csv)
            self._save_confusion_matrix(matrix, names)

            errors = test.loc[targets.to_numpy() != predictions].copy()
            errors["provided_label"] = targets[errors.index].map(config.label_mapping)
            errors["predicted_label"] = pd.Series(predictions, index=test.index)[errors.index].map(config.label_mapping)
            errors["predicted_probability"] = probabilities.max(axis=1)[targets.to_numpy() != predictions]
            count = int(config.parameters["error_sample_size"])
            if count < 0:
                raise ValueError("error_sample_size must be nonnegative")
            errors.sample(min(count, len(errors)), random_state=int(config.parameters["random_state"])).to_csv(
                config.error_sample_file, index=False, encoding=config.encoding,
            )
            report.update({
                "passed": True, "split": "test", "test_rows": len(test),
                "model_sha256": training["model_sha256"], "preprocessing_sha256": preprocessing_hash,
                "test_sha256": preprocessing["splits"]["test"]["sha256"], "test_metrics": test_metrics,
                "majority_baseline": baseline, "majority_label": training["majority_label"],
                "misclassified_rows": len(errors), "labels_manually_verified": False,
                "interpretation": "Metrics measure agreement with supplied dataset labels on unseen title groups.",
            })
            write_json(config.report_file, report)
            write_json(config.metrics_file, flat_metrics)
            if tracking:
                tracking.log_evaluation(config, report, flat_metrics, training)
            self.logger.info("Test accuracy: %.4f; macro F1: %.4f; majority baseline accuracy: %.4f",
                             test_metrics["accuracy"], test_metrics["f1_macro"], baseline["accuracy"])
            return report
        except Exception as error:
            report.update({"passed": False, "error": str(error)})
            write_json(config.report_file, report)
            write_json(config.metrics_file, {"evaluation_passed": 0})
            raise

    def _save_confusion_matrix(self, matrix, names):
        os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[3] / ".venv/.matplotlib"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(6.5, 5.5))
        ConfusionMatrixDisplay(matrix, display_labels=names).plot(ax=ax, cmap="Blues", values_format="d", colorbar=False)
        ax.set_title("TF-IDF logistic regression: held-out test set")
        fig.tight_layout()
        self.config.confusion_matrix_plot.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(self.config.confusion_matrix_plot, dpi=150)
        plt.close(fig)
