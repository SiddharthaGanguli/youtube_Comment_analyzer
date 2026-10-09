import json
import os
import tempfile
import unittest
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.Sentiment_analysis.components.model_evaluation import ModelEvaluation
from src.Sentiment_analysis.components.model_training import ModelTraining
from src.Sentiment_analysis.entity.entity import MLflowConfig, ModelEvaluationConfig
from src.Sentiment_analysis.utils.aws_credentials import aws_credentials
from src.Sentiment_analysis.utils.experiment_tracking import load_mlflow
from tests.fixtures import training_fixture


@contextmanager
def tracking_directory():
    with tempfile.TemporaryDirectory() as directory:
        try:
            yield directory
        finally:
            # SQLite pools hold files open on Windows even after a run finishes.
            from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore
            from mlflow.store.model_registry.sqlalchemy_store import SqlAlchemyStore as RegistryStore
            for store in (SqlAlchemyStore, RegistryStore):
                for engine in store._engine_map.values():
                    engine.dispose()


class TrackingTests(unittest.TestCase):
    def test_training_and_evaluation_share_a_run_and_model_reloads(self):
        with tracking_directory() as directory:
            root = Path(directory)
            settings = MLflowConfig(root, f"sqlite:///{(root / 'tracking.db').as_posix()}",
                                    "test-sentiment", (root / "mlflow-artifacts").as_uri(), "fixture")
            training, paths = training_fixture(root)
            training = replace(training, mlflow=settings)
            report = ModelTraining(training).run()
            mlflow = load_mlflow()
            run_id = report["mlflow"]["run_id"]
            model = mlflow.sklearn.load_model(report["mlflow"]["model_uri"])
            self.assertEqual(model.predict(["wonderful"]).tolist(), [2])
            import numpy as np
            serving_model = mlflow.pyfunc.load_model(report["mlflow"]["model_uri"])
            self.assertEqual(serving_model.predict(np.asarray(["wonderful"])).tolist(), [2])
            evaluation = ModelEvaluationConfig(
                paths["train"], paths["validation"], paths["test"], training.preprocessing_report,
                training.model_file, training.report_file, root / "evaluation.json", root / "metrics.json",
                root / "classes.json", root / "matrix.csv", root / "matrix.png", root / "errors.csv",
                "utf-8", "CommentText", "VideoTitle", "Sentiment_label", training.label_mapping,
                {"random_state": 42, "error_sample_size": 5}, settings,
            )
            result = ModelEvaluation(evaluation).run()
            self.assertEqual(result["mlflow"]["run_id"], run_id)
            run = mlflow.get_run(run_id)
            self.assertEqual(run.info.status, "FINISHED")
            self.assertEqual(run.data.metrics["test_accuracy"], 1)
            self.assertIn("validation_f1_macro", run.data.metrics)
            self.assertEqual(run.data.params["tfidf.min_df"], "1")
            client = mlflow.MlflowClient()
            artifacts = {item.path for item in client.list_artifacts(run_id, "stages/evaluation")}
            self.assertIn("stages/evaluation/matrix.png", artifacts)
            self.assertIn("stages/evaluation/evaluation.json", artifacts)
            evaluation.test_file.write_text("changed input")
            with self.assertRaisesRegex(ValueError, "Split checksum changed"):
                ModelEvaluation(evaluation).run()
            self.assertEqual(mlflow.get_run(run_id).info.status, "FAILED")
            self.assertEqual(mlflow.get_run(run_id).data.metrics["evaluation_passed"], 0)
            self.assertFalse(json.loads(evaluation.report_file.read_text())["passed"])

    def test_private_dvc_credentials_are_temporary_and_explicit_credentials_win(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dvc").mkdir()
            (root / ".dvc/config.local").write_text(
                "['remote \"storage\"']\naccess_key_id = fixture-key\nsecret_access_key = fixture-secret\n",
            )
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=root):
                with aws_credentials(root):
                    self.assertEqual(os.environ["AWS_ACCESS_KEY_ID"], "fixture-key")
                self.assertNotIn("AWS_ACCESS_KEY_ID", os.environ)
                os.environ["AWS_ACCESS_KEY_ID"] = "explicit-key"
                with aws_credentials(root):
                    self.assertEqual(os.environ["AWS_ACCESS_KEY_ID"], "explicit-key")


if __name__ == "__main__":
    unittest.main()
