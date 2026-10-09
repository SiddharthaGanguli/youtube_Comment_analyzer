import json
import warnings
from importlib.metadata import version

import joblib
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from threadpoolctl import threadpool_limits

from logging_.logger_ import Logger
from src.Sentiment_analysis.entity.entity import ModelTrainingConfig
from src.Sentiment_analysis.utils.common import (
    check_split_overlap, classification_metrics, load_verified_split, sha256_file, write_json,
)
from src.Sentiment_analysis.utils.experiment_tracking import experiment_run
from src.Sentiment_analysis.utils.text import text_preprocessing_settings


class ModelTraining:
    def __init__(self, config: ModelTrainingConfig):
        self.config = config
        self.logger = Logger("model_training.log").get_logger()

    def run(self):
        try:
            with experiment_run(self.config.mlflow) as tracking:
                return self._run(tracking)
        except Exception as error:
            # Setup/upload failures must invalidate any previous successful report too.
            write_json(self.config.report_file, {"passed": False, "error": str(error)})
            raise

    def _run(self, tracking):
        config = self.config
        report = {"passed": False}
        try:
            preprocessing = json.loads(config.preprocessing_report.read_text(encoding="utf-8"))
            if not preprocessing.get("passed"):
                raise ValueError("Successful preprocessing is required before training")
            text_settings = text_preprocessing_settings(preprocessing["parameters"])
            frames = {}
            for name, path in [("train", config.train_file), ("validation", config.validation_file)]:
                frames[name] = load_verified_split(
                    path, preprocessing["splits"][name], config.encoding, config.text_column,
                    config.title_column, config.target_column, config.label_mapping,
                )
            check_split_overlap(frames, config.text_column, config.title_column)
            train, validation = frames["train"], frames["validation"]

            params = config.parameters
            tfidf_params = dict(params["tfidf"])
            tfidf_params["ngram_range"] = tuple(tfidf_params["ngram_range"])
            vectorizer = TfidfVectorizer(**tfidf_params, dtype=np.float32)
            classifier = LogisticRegression(**params["logistic_regression"], random_state=int(params["random_state"]))
            pipeline = Pipeline([("tfidf", vectorizer), ("classifier", classifier)])
            self.logger.info("Fitting TF-IDF and logistic regression on %s training comments", len(train))
            # Pipeline.fit learns vocabulary/IDF and classifier weights using training rows only.
            with threadpool_limits(limits=int(params["thread_limit"])), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ConvergenceWarning)
                pipeline.fit(train[config.text_column], train[config.target_column])
            converged = not any(issubclass(warning.category, ConvergenceWarning) for warning in caught)
            if not converged:
                raise ValueError("Logistic regression did not converge; increase max_iter or review the tolerance")
            if not np.isfinite(classifier.coef_).all():
                raise ValueError("Classifier weights contain non-finite values")

            self.logger.info("Scoring the validation split; the test split has not been read")
            probabilities = pipeline.predict_proba(validation[config.text_column])
            predictions = classifier.classes_[probabilities.argmax(axis=1)]
            labels = sorted(config.label_mapping)
            validation_metrics = classification_metrics(validation[config.target_column], predictions, labels, probabilities)
            majority_label = int(train[config.target_column].value_counts().idxmax())
            baseline_metrics = classification_metrics(validation[config.target_column],
                                                      np.full(len(validation), majority_label), labels)
            preprocessing_sha256 = sha256_file(config.preprocessing_report)
            package_versions = {name: version(name) for name in ["scikit-learn", "numpy", "scipy", "joblib"]}
            bundle = {
                "pipeline": pipeline, "label_mapping": config.label_mapping,
                "text_column": config.text_column, "preprocessing_sha256": preprocessing_sha256,
                "parameters": params, "package_versions": package_versions,
                "text_preprocessing": text_settings,
            }
            config.model_file.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(bundle, config.model_file, compress=3)
            report.update({
                "passed": True, "model_type": "TF-IDF + LogisticRegression", "parameters": params,
                "preprocessing_sha256": preprocessing_sha256, "model_sha256": sha256_file(config.model_file),
                "train_rows": len(train), "validation_rows": len(validation),
                "vocabulary_size": len(vectorizer.vocabulary_), "converged": converged,
                "iterations": classifier.n_iter_.tolist(), "validation_metrics": validation_metrics,
                "majority_label": majority_label, "validation_majority_baseline": baseline_metrics,
                "label_mapping": config.label_mapping, "package_versions": package_versions,
                "test_used_for_training": False,
                "text_preprocessing": text_settings,
            })
            write_json(config.report_file, report)
            if tracking:
                tracking.log_training(pipeline, config, report)
            self.logger.info("Validation accuracy: %.4f; macro F1: %.4f",
                             validation_metrics["accuracy"], validation_metrics["f1_macro"])
            return report
        except Exception as error:
            report.update({"passed": False, "error": str(error)})
            write_json(config.report_file, report)
            raise
