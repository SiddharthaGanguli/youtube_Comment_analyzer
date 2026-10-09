import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.Sentiment_analysis.components.data_preprocessing import DataPreprocessing
from src.Sentiment_analysis.components.model_training import ModelTraining
from src.Sentiment_analysis.entity.entity import ModelPredictionConfig
from src.Sentiment_analysis.services.prediction import PredictionService
from src.Sentiment_analysis.utils.text import normalize_text
from tests.fixtures import training_fixture


class PredictionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.training, _ = training_fixture(cls.root)
        ModelTraining(cls.training).run()
        cls.config = ModelPredictionConfig(
            cls.root, cls.training.model_file, cls.training.report_file, cls.root / "predictions.json",
            "CommentText", cls.training.label_mapping, False,
            {"batch_size": 2, "max_comments": 100, "review_confidence_threshold": .6},
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_training_and_prediction_share_normalization(self):
        raw = ["  ｗｏｎｄｅｒｆｕｌ\nvideo &#x1F60A; ", "I&#39;m NOT unhappy! &amp; 😊", "&amp;lt;3"]
        preprocessing = object.__new__(DataPreprocessing)
        preprocessing.config = type("Config", (), {"parameters": {"unicode_normalization": "NFKC"}})()
        prepared = preprocessing._normalize(pd.Series(raw, dtype="string")).tolist()
        self.assertEqual(prepared, ["wonderful video 😊", "I'm NOT unhappy! & 😊", "&lt;3"])
        self.assertEqual([normalize_text(text) for text in raw], prepared)
        service = PredictionService(self.config)
        result = service.predict(raw)
        self.assertEqual([row["normalized_text"] for row in result["comments"]], prepared)
        direct = service.pipeline.predict_proba(prepared)
        for row, scores in zip(result["comments"], direct):
            if row["status"] == "analyzed":
                np.testing.assert_allclose(list(row["class_probabilities"].values()), scores, atol=1e-7)

    def test_batching_mapping_skips_and_order(self):
        comments = ["wonderful video", None, "ordinary video", "   ", "terrible video", "qzxvplmnbvcxzq"]
        with patch("src.Sentiment_analysis.services.prediction.joblib.load", wraps=__import__("joblib").load) as load:
            service = PredictionService(self.config)
            result = service.predict(comments)
            service.predict(comments[:1])
            self.assertEqual(load.call_count, 1)
        summary = result["summary"]
        self.assertEqual((summary["received"], summary["analyzed"], summary["skipped"]), (6, 3, 3))
        self.assertEqual(summary["skipped_by_reason"], {"empty_text": 2, "no_known_features": 1})
        self.assertEqual(summary["sentiment_counts"], {"Negative": 1, "Neutral": 1, "Positive": 1})
        self.assertEqual([row["index"] for row in result["comments"]], list(range(6)))
        for row in result["comments"]:
            if row["status"] == "analyzed":
                self.assertEqual(row["sentiment"], self.config.label_mapping[row["sentiment_label"]])
                self.assertAlmostEqual(sum(row["class_probabilities"].values()), 1)
                self.assertEqual(row["needs_review"], row["confidence"] < .6)
            else:
                self.assertIsNone(row["confidence"])
                self.assertIsNone(row["sentiment"])
        json.dumps(result, allow_nan=False)

    def test_empty_and_invalid_requests_and_limits(self):
        service = PredictionService(self.config)
        self.assertEqual(service.predict([])["summary"]["analyzed"], 0)
        for invalid in ["one comment", [42], [{}], {"comments": []}]:
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                service.predict(invalid)
        with self.assertRaises(ValueError):
            service.predict(["hello"] * 101)
        for change in [{"batch_size": 0}, {"max_comments": True}, {"review_confidence_threshold": float("nan")}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                PredictionService(replace(self.config, parameters={**self.config.parameters, **change}))

    def test_checksum_is_checked_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "model.joblib"
            corrupt.write_bytes(b"not the trained model")
            with patch("src.Sentiment_analysis.services.prediction.joblib.load") as load:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    PredictionService(replace(self.config, model_file=corrupt))
                load.assert_not_called()

    def test_saved_recipe_and_metadata_guard(self):
        params = {**self.config.parameters, "unicode_normalization": "NFC"}
        service = PredictionService(replace(self.config, parameters=params))
        self.assertEqual(service.predict(["ｗｏｎｄｅｒｆｕｌ"])["comments"][0]["normalized_text"], "wonderful")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "training.json"
            report = json.loads(self.training.report_file.read_text())
            report["text_preprocessing"]["version"] = 2
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "preprocessing recipe"):
                PredictionService(replace(self.config, training_report=path))
            report["text_preprocessing"]["version"] = 1
            report["label_mapping"]["2"] = "Wrong label"
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "label mapping"):
                PredictionService(replace(self.config, training_report=path))

    def test_missing_artifacts_can_be_restored_with_dvc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self.config, project_root=root, model_file=root / "model.joblib",
                             training_report=root / "training.json", restore_with_dvc=True)
            def restore(*args, **kwargs):
                shutil.copyfile(self.training.model_file, config.model_file)
                shutil.copyfile(self.training.report_file, config.training_report)
            with patch("src.Sentiment_analysis.services.prediction.subprocess.run", side_effect=restore) as pull:
                result = PredictionService(config).predict(["wonderful"])
                self.assertEqual(result["summary"]["analyzed"], 1)
                self.assertEqual(pull.call_args.args[0][1:4], ["-m", "dvc", "pull"])
                self.assertEqual(pull.call_args.kwargs["cwd"], root)
        with self.assertRaises(FileNotFoundError):
            PredictionService(replace(self.config, model_file=self.root / "missing.joblib"))
