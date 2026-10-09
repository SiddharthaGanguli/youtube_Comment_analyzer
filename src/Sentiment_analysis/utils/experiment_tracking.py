"""Explicit experiment logging: training uses validation; evaluation adds test results."""

import json
import os
import subprocess
from contextlib import contextmanager
from importlib.metadata import version

from src.Sentiment_analysis.utils.aws_credentials import aws_credentials
from src.Sentiment_analysis.utils.common import write_json


def flatten_parameters(values, prefix=""):
    result = {}
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten_parameters(value, name))
        else:
            result[name] = json.dumps(value) if isinstance(value, (list, tuple)) else value
    return result


def load_mlflow():
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "true")
    import mlflow
    return mlflow


@contextmanager
def experiment_run(settings, previous=None):
    if settings is None:
        yield None
        return
    mlflow = load_mlflow()
    mlflow.set_tracking_uri(settings.tracking_uri)
    if mlflow.active_run():
        raise RuntimeError("An MLflow run is already active; finish it before running this pipeline")
    with aws_credentials(settings.project_root):
        if previous:
            if previous["tracking_uri"] != settings.tracking_uri:
                raise ValueError("Evaluation must use the training run's MLflow tracking database")
            run = mlflow.get_run(previous["run_id"])
            experiment = mlflow.get_experiment(run.info.experiment_id)
            if experiment.name != settings.experiment_name or experiment.artifact_location != settings.artifact_location:
                raise ValueError("Training run belongs to a different MLflow experiment or artifact location")
            with mlflow.start_run(run_id=previous["run_id"]):
                mlflow.set_tag("mlflow.user", settings.user_name)
                try:
                    yield ExperimentTracking(mlflow, settings)
                except Exception:
                    mlflow.set_tag("evaluation_passed", "false")
                    mlflow.log_metric("evaluation_passed", 0)
                    raise
        else:
            experiment = mlflow.get_experiment_by_name(settings.experiment_name)
            if experiment is None:
                experiment_id = mlflow.create_experiment(
                    settings.experiment_name, artifact_location=settings.artifact_location,
                )
            else:
                if experiment.artifact_location != settings.artifact_location:
                    raise ValueError("Existing MLflow experiment has a different artifact location")
                experiment_id = experiment.experiment_id
            with mlflow.start_run(experiment_id=experiment_id, run_name=settings.run_name,
                                  tags={"mlflow.user": settings.user_name}):
                yield ExperimentTracking(mlflow, settings)


class ExperimentTracking:
    def __init__(self, mlflow, settings):
        self.mlflow = mlflow
        self.settings = settings

    def log_training(self, pipeline, config, report):
        from mlflow.models import infer_signature
        import numpy as np
        mlflow = self.mlflow
        mlflow.log_params(flatten_parameters(config.parameters))
        mlflow.log_params({"train_rows": report["train_rows"], "validation_rows": report["validation_rows"],
                           "vocabulary_size": report["vocabulary_size"]})
        mlflow.set_tags({"model_type": report["model_type"], "preprocessing_sha256": report["preprocessing_sha256"],
                         "model_sha256": report["model_sha256"], "test_used_for_training": "false"})
        for arguments, tag in [(["rev-parse", "HEAD"], "git_commit"), (["status", "--porcelain"], "git_worktree")]:
            result = subprocess.run(["git", *arguments], cwd=self.settings.project_root,
                                    capture_output=True, text=True, check=False)
            if result.returncode == 0:
                mlflow.set_tag(tag, result.stdout.strip() if tag == "git_commit" else
                               ("dirty" if result.stdout.strip() else "clean"))
        mlflow.log_metrics({f"validation_{key}": value for key, value in report["validation_metrics"].items()})
        mlflow.log_metrics({f"validation_majority_baseline_{key}": value
                            for key, value in report["validation_majority_baseline"].items()})
        # A string tensor keeps the pyfunc input one-dimensional, as TF-IDF expects.
        example = np.asarray(["I enjoyed this video.", "This was not helpful."])
        model = mlflow.sklearn.log_model(
            pipeline, name="sentiment_model", input_example=example, serialization_format="cloudpickle",
            signature=infer_signature(example, pipeline.predict(example)),
            pip_requirements=[f"{name}=={version(name)}" for name in
                              ("scikit-learn", "numpy", "scipy", "joblib", "cloudpickle")],
            metadata={"label_mapping": {str(key): value for key, value in config.label_mapping.items()},
                      "preprocessing_sha256": report["preprocessing_sha256"]},
            tags={"mlflow.user": self.settings.user_name},
        )
        run = mlflow.active_run()
        report["mlflow"] = {"run_id": run.info.run_id, "experiment_id": run.info.experiment_id,
                            "tracking_uri": self.settings.tracking_uri, "artifact_uri": run.info.artifact_uri,
                            "model_uri": model.model_uri}
        write_json(config.report_file, report)
        for path in (config.model_file, config.report_file):
            mlflow.log_artifact(str(path), "stages/training")
        mlflow.log_artifact(str(config.preprocessing_report), "stages/preprocessing")
        for stage, filename in [("ingestion", "data_ingestion/ingestion_report.json"),
                                ("validation", "data_validation/validation_report.json")]:
            path = self.settings.project_root / "artifacts" / filename
            if path.is_file():
                mlflow.log_artifact(str(path), f"stages/{stage}")
        for filename in ("config/config.yaml", "config/params.yaml", "dvc.yaml", "requirements-pipeline.txt",
                         "requirements-tracking.txt"):
            path = self.settings.project_root / filename
            if path.is_file():
                mlflow.log_artifact(str(path), "source")
        for filename in (
            "components/model_training.py", "components/model_evaluation.py", "utils/common.py",
            "utils/experiment_tracking.py", "utils/aws_credentials.py", "entity/entity.py", "config/config.py",
        ):
            path = self.settings.project_root / "src/Sentiment_analysis" / filename
            if path.is_file():
                mlflow.log_artifact(str(path), f"source/code/{path.parent.name}")

    def log_evaluation(self, config, report, metrics, training):
        mlflow = self.mlflow
        run = mlflow.active_run()
        if run.data.tags.get("model_sha256") != training["model_sha256"]:
            raise ValueError("MLflow run model checksum differs from the evaluated model")
        report["mlflow"] = training["mlflow"]
        write_json(config.report_file, report)
        write_json(config.metrics_file, metrics)
        mlflow.log_metrics(metrics)
        mlflow.set_tags({"evaluation_passed": "true", "test_sha256": report["test_sha256"],
                         "labels_manually_verified": "false"})
        for path in (config.report_file, config.metrics_file, config.classification_report_file,
                     config.confusion_matrix_csv, config.confusion_matrix_plot, config.error_sample_file):
            mlflow.log_artifact(str(path), "stages/evaluation")
