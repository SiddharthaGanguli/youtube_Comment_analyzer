"""Prepare the worker's verified bundle without restoring DVC or loading weights."""

import json

from src.Sentiment_analysis.entity.analysis import LABELS, ModelReference
from src.Sentiment_analysis.entity.entity import ModelPredictionConfig
from src.Sentiment_analysis.entity.worker import WorkerConfig, WorkerIdentity


class WorkerConfigurationManager:
    def __init__(self, settings, *, image_code_revision):
        self.settings = settings
        self.image_code_revision = image_code_revision

    def get_worker_config(self):
        settings = self.settings
        model = settings.model
        if model.release_id is None or model.code_revision is None:
            raise ValueError("Assign and verify an immutable serving release before starting a worker")
        if self.image_code_revision != model.code_revision:
            raise ValueError("Worker image code revision differs from its assigned release")
        identity = WorkerIdentity(code_revision=self.image_code_revision, model=ModelReference(
            release_id=model.release_id, sha256=model.sha256, preprocessing_sha256=model.preprocessing_sha256,
            review_confidence_threshold=settings.worker.review_confidence_threshold,
            result_schema_version=model.result_schema_version))
        if not model.model_file.is_file() or not model.training_report.is_file():
            raise FileNotFoundError("Package the verified model and training report in the worker image")
        if model.training_report.stat().st_size > 1048576:
            raise ValueError("Training report exceeds the worker metadata limit")
        report = json.loads(model.training_report.read_text(encoding="utf-8"))
        if (report.get("passed") is not True or report.get("model_sha256") != model.sha256 or
                report.get("preprocessing_sha256") != model.preprocessing_sha256 or
                report.get("model_type") != "TF-IDF + LogisticRegression" or
                report.get("label_mapping") != {str(k): v for k, v in LABELS.items()}):
            raise ValueError("Packaged training report differs from the assigned serving release")
        prediction = ModelPredictionConfig(
            project_root=model.model_file.parent, model_file=model.model_file, training_report=model.training_report,
            report_file=model.model_file.parent / "unused-worker-predictions.json", text_column=model.text_column,
            label_mapping=dict(LABELS), restore_with_dvc=False,
            parameters={"batch_size": settings.worker.batch_size, "max_comments": settings.youtube.maximum_comments,
                        "review_confidence_threshold": settings.worker.review_confidence_threshold})
        return WorkerConfig(settings, prediction, identity)
