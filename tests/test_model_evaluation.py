import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.Sentiment_analysis.components.model_evaluation import ModelEvaluation
from src.Sentiment_analysis.components.model_training import ModelTraining
from src.Sentiment_analysis.entity.entity import ModelEvaluationConfig
from tests.fixtures import training_fixture


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        training, paths = training_fixture(root)
        ModelTraining(training).run()
        self.config = ModelEvaluationConfig(
            paths["train"], paths["validation"], paths["test"], training.preprocessing_report,
            training.model_file, training.report_file, root / "evaluation.json", root / "metrics.json",
            root / "classes.json", root / "matrix.csv", root / "matrix.png", root / "errors.csv",
            "utf-8", "CommentText", "VideoTitle", "Sentiment_label", training.label_mapping,
            {"random_state": 42, "error_sample_size": 5},
        )

    def test_test_set_scoring_and_confusion_matrix_are_consistent(self):
        report = ModelEvaluation(self.config).run()
        metrics = json.loads(self.config.metrics_file.read_text())
        matrix = pd.read_csv(self.config.confusion_matrix_csv, index_col=0)
        self.assertEqual(report["split"], "test")
        self.assertEqual(metrics["test_rows"], 18)
        self.assertEqual(metrics["test_accuracy"], 1)
        self.assertAlmostEqual(metrics["majority_baseline_accuracy"], 1 / 3)
        self.assertEqual(matrix.to_numpy().sum(), 18)
        self.assertTrue((matrix.to_numpy().diagonal() == 6).all())
        self.assertTrue(self.config.confusion_matrix_plot.is_file())

    def test_modified_model_is_rejected_before_unpickling(self):
        self.config.model_file.write_bytes(b"changed model")
        with patch("src.Sentiment_analysis.components.model_evaluation.joblib.load") as load:
            with self.assertRaisesRegex(ValueError, "Model checksum changed"):
                ModelEvaluation(self.config).run()
            load.assert_not_called()

    def test_modified_test_split_replaces_stale_metrics_with_failure(self):
        ModelEvaluation(self.config).run()
        self.config.test_file.write_text("changed test")
        with self.assertRaisesRegex(ValueError, "Split checksum changed"):
            ModelEvaluation(self.config).run()
        self.assertEqual(json.loads(self.config.metrics_file.read_text()), {"evaluation_passed": 0})
