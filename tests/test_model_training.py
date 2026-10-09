import tempfile
import unittest
from pathlib import Path

import joblib

from src.Sentiment_analysis.components.model_training import ModelTraining
from tests.fixtures import training_fixture


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config, self.paths = training_fixture(Path(self.directory.name))

    def test_heldout_vocabulary_is_not_fitted_and_model_can_be_reloaded(self):
        report = ModelTraining(self.config).run()
        bundle = joblib.load(self.config.model_file)
        vocabulary = bundle["pipeline"].named_steps["tfidf"].vocabulary_
        self.assertIn("trainonlyquokka", vocabulary)
        self.assertNotIn("validationonlyquokka", vocabulary)
        self.assertNotIn("testonlyquokka", vocabulary)
        self.assertIn("😊", vocabulary)
        self.assertIn("not", vocabulary)
        self.assertTrue(report["converged"])
        self.assertEqual(bundle["pipeline"].predict(["wonderful mood 😊"])[0], 2)

    def test_edited_training_split_is_rejected(self):
        self.config.train_file.write_text("changed")
        with self.assertRaisesRegex(ValueError, "checksum changed"):
            ModelTraining(self.config).run()
