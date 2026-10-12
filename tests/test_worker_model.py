"""Real shared-model integration on a tiny trained fixture; no live network."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.Sentiment_analysis.components.model_training import ModelTraining
from src.Sentiment_analysis.config.worker import WorkerConfigurationManager
from src.Sentiment_analysis.config.worker_runtime import build_worker_pipeline
from src.Sentiment_analysis.entity.analysis import AnalysisInput, ModelReference
from src.Sentiment_analysis.entity.api import release_fingerprint
from src.Sentiment_analysis.integrations.fixture_comments import FixtureCommentProvider
from src.Sentiment_analysis.services.prediction import PredictionService
from tests.fixtures import training_fixture
from tests.serving.aws_fixtures import ROOT, StorageFixture


class WorkerModelTests(StorageFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.training, _ = training_fixture(cls.root)
        cls.training = replace(cls.training, parameters={**cls.training.parameters, "thread_limit": 4})
        ModelTraining(cls.training).run()
        cls.report = json.loads(cls.training.report_file.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        super().setUp()
        code = "4" * 40
        model = ModelReference(release_id="sr_" + "0" * 64, sha256=self.report["model_sha256"],
                               preprocessing_sha256=self.report["preprocessing_sha256"],
                               review_confidence_threshold=.6, result_schema_version=1)
        model = model.model_copy(update={"release_id": release_fingerprint(model, code)})
        self.config = self.config.model_copy(update={"model": self.config.model.model_copy(update={
            "model_file": self.training.model_file, "training_report": self.training.report_file,
            "sha256": model.sha256, "preprocessing_sha256": model.preprocessing_sha256,
            "release_id": model.release_id, "code_revision": code}),
            "worker": self.config.worker.model_copy(update={"batch_size": 2}),
            "youtube": self.config.youtube.model_copy(update={"global_http_attempts_per_second": 100})})
        self.jobs.settings, self.objects.settings, self.model = self.config, self.config, model
        self.data = json.loads((ROOT / "examples/worker-fixture.json").read_text(encoding="utf-8"))

    def test_real_model_worker_matches_direct_shared_predictions(self):
        provider = FixtureCommentProvider(self.config, self.data)
        normal = PredictionService(WorkerConfigurationManager(self.config, image_code_revision="4" * 40).get_worker_config().prediction)
        self.assertEqual(normal.native_thread_limit, 4)
        with patch("src.Sentiment_analysis.services.prediction.joblib.load", wraps=__import__("joblib").load) as load, \
                patch("src.Sentiment_analysis.services.prediction.subprocess.run", side_effect=AssertionError("DVC must stay disabled")):
            pipeline = build_worker_pipeline(self.config, self.jobs, self.objects, provider, image_code_revision="4" * 40)
            work = self.admit().work
            self.assertTrue(pipeline.process(self.message(work)))
            current = self.jobs.get_work(work.work_id)
            result = self.objects.read(current, current.result)
            expected = normal.predict([row["text"] for row in result["comments"]])
            for actual, predicted in zip(result["comments"], expected["comments"], strict=True):
                for key in ["index", "normalized_text", "sentiment", "sentiment_label", "class_probabilities", "skipped_reason", "needs_review"]:
                    self.assertEqual(actual[key], predicted[key])
            other = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=998)).work
            self.assertTrue(pipeline.process(self.message(other)))
            self.assertEqual(load.call_count, 1)
            self.assertEqual(pipeline.processor.predictor.native_thread_limit, 1)

    def test_bundle_checks_precede_deserialization_and_no_dvc_restore(self):
        with patch("src.Sentiment_analysis.services.prediction.joblib.load") as load:
            wrong = self.config.model_copy(update={"model": self.config.model.model_copy(update={"preprocessing_sha256": "9" * 64})})
            with self.assertRaises(ValueError):
                WorkerConfigurationManager(wrong, image_code_revision="4" * 40).get_worker_config()
            with self.assertRaises(ValueError):
                build_worker_pipeline(self.config, self.jobs, self.objects, FixtureCommentProvider(self.config, self.data), image_code_revision="5" * 40)
            load.assert_not_called()
        bad_file = self.root / "corrupt.joblib"
        bad_file.write_bytes(b"corrupted fixture model")
        corrupt = self.config.model_copy(update={"model": self.config.model.model_copy(update={"model_file": bad_file})})
        with patch("src.Sentiment_analysis.services.prediction.joblib.load") as load, self.assertRaisesRegex(ValueError, "checksum"):
            build_worker_pipeline(corrupt, self.jobs, self.objects, FixtureCommentProvider(self.config, self.data), image_code_revision="4" * 40)
        load.assert_not_called()
        missing = self.config.model_copy(update={"model": self.config.model.model_copy(update={"model_file": self.root / "missing.joblib"})})
        with patch("src.Sentiment_analysis.services.prediction.subprocess.run") as pull, self.assertRaises(FileNotFoundError):
            build_worker_pipeline(missing, self.jobs, self.objects, FixtureCommentProvider(self.config, self.data), image_code_revision="4" * 40)
        pull.assert_not_called()
        bad_report = self.root / "wrong-packages.json"
        bad_report.write_text(json.dumps({**self.report, "package_versions": {"scikit-learn": "0.0.0"}}), encoding="utf-8")
        wrong_versions = self.config.model_copy(update={"model": self.config.model.model_copy(update={"training_report": bad_report})})
        with patch("src.Sentiment_analysis.services.prediction.joblib.load") as load, self.assertRaisesRegex(ValueError, "scikit-learn"):
            build_worker_pipeline(wrong_versions, self.jobs, self.objects, FixtureCommentProvider(self.config, self.data), image_code_revision="4" * 40)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
