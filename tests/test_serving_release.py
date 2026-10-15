import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from scripts.package_serving import golden_examples, write_zip
from scripts.deploy_serving import checked_release, deploy_release, release_manifest, web_settings
from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.entity.analysis import ModelReference
from src.Sentiment_analysis.entity.api import release_fingerprint


class ServingReleaseTests(unittest.TestCase):
    def test_package_contains_only_runtime_files_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged"; staged.mkdir()
            (staged / "runtime.py").write_text("value = 1\n", encoding="utf-8")
            for name in ("tests/private.py", "__pycache__/runtime.pyc", "bin/tool"):
                path = staged / name; path.parent.mkdir(exist_ok=True); path.write_text("exclude", encoding="utf-8")
            (staged / "library.dist-info").mkdir()
            (staged / "library.dist-info/METADATA").write_text("Version: 1\n", encoding="utf-8")
            result = write_zip(staged, root / "api.zip")
            with zipfile.ZipFile(root / "api.zip") as archive:
                self.assertEqual(set(archive.namelist()), {"runtime.py", "library.dist-info/METADATA"})
            self.assertEqual(result["sha256"], hashlib.sha256((root / "api.zip").read_bytes()).hexdigest())

    def test_golden_examples_bind_model_and_threshold_without_tracking_metadata(self):
        prediction = {"model": {"sha256": "a" * 64, "review_confidence_threshold": .6, "mlflow_run_id": "private"},
                      "comments": [{"text": "Great explanation", "status": "analyzed", "sentiment": "Positive",
                                    "skipped_reason": None, "class_probabilities": {"Positive": .8, "Neutral": .1, "Negative": .1}}]}
        result = golden_examples(prediction, "a" * 64, .6)
        self.assertEqual(result["inputs"], ["Great explanation"])
        self.assertNotIn("private", json.dumps(result))
        with self.assertRaises(ValueError): golden_examples(prediction, "b" * 64, .6)
        with self.assertRaises(ValueError): golden_examples(prediction, "a" * 64, .7)

    def test_deploy_package_check_rejects_changed_zip_or_wrong_release(self):
        settings = ServingConfigurationManager().get_serving_config()
        model = ModelReference(release_id="sr_" + "0" * 64, sha256=settings.model.sha256,
            preprocessing_sha256=settings.model.preprocessing_sha256, review_confidence_threshold=.6, result_schema_version=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"schema_version": 1, "dependencies_installed": True, "code_revision": "a" * 40,
                "model_sha256": settings.model.sha256, "release_id": release_fingerprint(model, "a" * 40), "packages": {}}
            for name in ("api", "worker"):
                path = root / f"{name}.zip"; path.write_bytes(b"package")
                manifest["packages"][name] = {"file": path.name, "sha256": hashlib.sha256(b"package").hexdigest(), "uncompressed_bytes": 10}
            (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(checked_release(root)[0]["release_id"], manifest["release_id"])
            (root / "worker.zip").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed"): checked_release(root)
            (root / "worker.zip").write_bytes(b"package")
            manifest["release_id"] = "sr_" + "1" * 64
            (root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint"): checked_release(root)

    def test_web_outputs_contain_only_public_client_metadata(self):
        config = web_settings({"ApiUrl": "https://api.example.com", "CognitoDomain": "https://login.example.com",
            "PublicClientId": "public", "ResultBaseUrl": "https://bucket.example.com/serving/development",
            "SecretArn": "must-not-copy"}, "https://example.com/dashboard.html")
        self.assertNotIn("must-not-copy", json.dumps(config))
        self.assertEqual(config["maxComments"], 1000)

    def test_existing_release_cannot_be_replaced_with_different_package_bytes(self):
        manifest = {"release_id": "sr_" + "a" * 64,
                    "packages": {name: {"sha256": "b" * 64} for name in ("api", "worker")}}
        cfn, s3 = Mock(), Mock()
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
            {"OutputKey": "ServingReleaseId", "OutputValue": manifest["release_id"]}],
            "Parameters": [{"ParameterKey": "ApiCodeKey", "ParameterValue": "older-package.zip"}]}]}
        session = Mock(); session.client.side_effect = lambda name: {"cloudformation": cfn, "s3": s3}[name]
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "template.json"
            template.write_text(json.dumps({"Metadata": {"CommentLensReleaseWorkflow": "first-development-release"}}), encoding="utf-8")
            with patch("scripts.deploy_serving.checked_release", return_value=(manifest, None, None)):
                with self.assertRaisesRegex(ValueError, "immutable packages"):
                    deploy_release(session, directory, template)
        s3.upload_file.assert_not_called()
        cfn.update_stack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
