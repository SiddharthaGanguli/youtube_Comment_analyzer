"""Live wiring uses synthetic keys and mocked AWS; no Google request is made."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.config.worker_runtime import worker_runtime
from src.Sentiment_analysis.config.youtube import load_youtube_api_key, validate_live_youtube
from src.Sentiment_analysis.integrations.fixture_comments import FixtureCommentProvider
from src.Sentiment_analysis.integrations.youtube import YouTubeCommentProvider

ROOT = Path(__file__).resolve().parents[2]
KEY = "synthetic_youtube_api_key_12345"
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:youtube-api-key-synthetic"
RUNTIME = "src.Sentiment_analysis.config.worker_runtime"


def live_settings():
    settings = ServingConfigurationManager().get_serving_config()
    youtube = settings.youtube.model_copy(update={
        "live_enabled": True, "analytics_use_approved": True,
        "approval_reference": "synthetic test approval", "api_key_secret_arn": SECRET,
        "project_quota_verified": True,
    })
    storage = settings.storage.model_copy(update={"table_name": "serving-test"})
    return settings.model_copy(update={"youtube": youtube, "storage": storage})


class YouTubeSecretTests(unittest.TestCase):
    def setUp(self):
        self.settings = live_settings()
        self.client = MagicMock()

    def assert_safe(self, action):
        with self.assertRaises(ValueError) as caught:
            action()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(caught.exception))
        return caught.exception

    def test_plain_key_and_exact_json_key_are_supported(self):
        for value in (KEY, json.dumps({"api_key": KEY})):
            with self.subTest(format=value.startswith("{")):
                self.client.reset_mock()
                self.client.get_secret_value.return_value = {"SecretString": value}
                self.assertEqual(load_youtube_api_key(self.settings, self.client), KEY)
                self.client.get_secret_value.assert_called_once_with(SecretId=SECRET)

    def test_malformed_secret_shapes_and_size_are_rejected_safely(self):
        cases = [None, [], {}, {"SecretBinary": b"binary"},
                 {"SecretString": KEY, "SecretBinary": b"binary"},
                 {"SecretString": None}, {"SecretString": 42},
                 {"SecretString": ""}, {"SecretString": " " + KEY},
                 {"SecretString": KEY + "\n"}, {"SecretString": "short"},
                 {"SecretString": "x" * 257}, {"SecretString": "x" * 4097},
                 {"SecretString": "é" * 2049}, {"SecretString": KEY + ":"},
                 {"SecretString": "{"}, {"SecretString": "[]"},
                 {"SecretString": json.dumps({"api_key": KEY, "other": KEY})},
                 {"SecretString": '{"api_key":"' + KEY + '","api_key":"duplicate"}'},
                 {"SecretString": json.dumps({"api_key": None})},
                 {"SecretString": json.dumps({"api_key": [KEY]})},
                 {"SecretString": json.dumps({"api_key": 42})}]
        for response in cases:
            with self.subTest(secret_type=type(response).__name__):
                self.client.get_secret_value.return_value = response
                self.assert_safe(lambda: load_youtube_api_key(self.settings, self.client))

    def test_secret_string_byte_cap_includes_json_whitespace_and_unicode(self):
        self.client.get_secret_value.return_value = {"SecretString": " " * 4096 + json.dumps({"api_key": KEY})}
        self.assert_safe(lambda: load_youtube_api_key(self.settings, self.client))
        self.client.get_secret_value.return_value = {"SecretString": "{" + '"api_key":"' + KEY + '",' + '"x":"' + "é" * 2048 + '"}'}
        self.assert_safe(lambda: load_youtube_api_key(self.settings, self.client))

    def test_aws_failure_never_echoes_credential_or_upstream_exception(self):
        self.client.get_secret_value.side_effect = OSError("AWS private diagnostic " + KEY)
        error = self.assert_safe(lambda: load_youtube_api_key(self.settings, self.client))
        self.assertEqual(str(error), "YouTube API key secret is unavailable")

    def test_every_readiness_gate_is_revalidated_before_secret_access(self):
        for changes in ({"live_enabled": False}, {"analytics_use_approved": False},
                        {"approval_reference": None}, {"approval_reference": "   "},
                        {"api_key_secret_arn": None}, {"project_quota_verified": False},
                        {"live_enabled": 1}, {"maximum_response_bytes": 0},
                        {"api_key_secret_arn": SECRET.replace("us-east-1", "us-west-2")}):
            settings = self.settings.model_copy(update={"youtube": self.settings.youtube.model_copy(update=changes)})
            with self.subTest(fields=tuple(changes)):
                self.client.reset_mock()
                self.assert_safe(lambda: load_youtube_api_key(settings, self.client))
                self.client.get_secret_value.assert_not_called()

    def test_validated_live_settings_are_a_fresh_typed_value(self):
        options = validate_live_youtube(self.settings)
        self.assertEqual(options, self.settings.youtube)
        self.assertIsNot(options, self.settings.youtube)


class YouTubeWorkerRuntimeTests(unittest.TestCase):
    def setUp(self):
        worker_runtime.cache_clear()
        self.addCleanup(worker_runtime.cache_clear)
        self.settings = live_settings()
        self.session = MagicMock()
        self.secrets = MagicMock()
        self.secrets.get_secret_value.return_value = {"SecretString": json.dumps({"api_key": KEY})}
        self.session.client.side_effect = lambda service, **kwargs: self.secrets if service == "secretsmanager" else MagicMock()
        self.environ = {key: value for key, value in os.environ.items()
                        if key not in {"SERVING_FIXTURE_PATH", "SERVING_CONFIG_PATH"}}
        self.environ["SERVING_IMAGE_CODE_REVISION"] = "4" * 40

    def wiring(self, settings=None):
        manager = MagicMock()
        manager.get_serving_config.return_value = settings or self.settings
        return manager

    def test_live_runtime_uses_role_profile_secret_and_reuses_warm_pipeline(self):
        pipeline = object()
        with patch.dict(os.environ, self.environ, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring()), \
                patch("boto3.Session", return_value=self.session) as constructor, \
                patch(RUNTIME + ".build_worker_pipeline", return_value=pipeline) as build:
            self.assertIs(worker_runtime(), pipeline)
            self.assertIs(worker_runtime(), pipeline)
        constructor.assert_called_once_with(region_name="us-east-1")
        self.secrets.get_secret_value.assert_called_once_with(SecretId=SECRET)
        self.assertEqual([call.args[0] for call in self.session.client.call_args_list], ["secretsmanager", "dynamodb", "s3"])
        sdk = self.session.client.call_args_list[0].kwargs["config"]
        self.assertEqual((sdk.connect_timeout, sdk.read_timeout), (2, 5))
        self.assertEqual(sdk.retries["total_max_attempts"], 3)
        build.assert_called_once()
        self.assertEqual(build.call_args.kwargs["image_code_revision"], "4" * 40)
        provider = build.call_args.args[3]
        self.assertIsInstance(provider, YouTubeCommentProvider)
        self.assertFalse(provider.is_fixture)
        self.assertNotIn(KEY, repr(provider))

    def test_development_fixture_never_reads_youtube_secret(self):
        fixture_settings = ServingConfigurationManager().get_serving_config()
        fixture_settings = fixture_settings.model_copy(update={"storage": self.settings.storage})
        env = {**self.environ, "SERVING_FIXTURE_PATH": str(ROOT / "examples/worker-fixture.json")}
        with patch.dict(os.environ, env, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring(fixture_settings)), \
                patch("boto3.Session", return_value=self.session), \
                patch(RUNTIME + ".build_worker_pipeline", return_value=object()) as build:
            worker_runtime()
        self.secrets.get_secret_value.assert_not_called()
        self.assertEqual([call.args[0] for call in self.session.client.call_args_list], ["dynamodb", "s3"])
        self.assertIsInstance(build.call_args.args[3], FixtureCommentProvider)

    def test_disabled_live_runtime_fails_before_aws_or_model_load(self):
        disabled = self.settings.model_copy(update={"youtube": ServingConfigurationManager().get_serving_config().youtube})
        with patch.dict(os.environ, self.environ, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring(disabled)), \
                patch("boto3.Session") as constructor, patch(RUNTIME + ".build_worker_pipeline") as build:
            with self.assertRaises(ValueError):
                worker_runtime()
        constructor.assert_not_called()
        build.assert_not_called()

    def test_copied_invalid_live_settings_fail_before_aws(self):
        invalid = self.settings.model_copy(update={"youtube": self.settings.youtube.model_copy(update={"project_quota_verified": False})})
        with patch.dict(os.environ, self.environ, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring(invalid)), \
                patch("boto3.Session") as constructor:
            with self.assertRaises(ValueError):
                worker_runtime()
        constructor.assert_not_called()

    def test_production_fixture_is_rejected_before_aws(self):
        production = self.settings.model_copy(update={"environment": "production"})
        env = {**self.environ, "SERVING_FIXTURE_PATH": str(ROOT / "examples/worker-fixture.json")}
        with patch.dict(os.environ, env, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring(production)), \
                patch("boto3.Session") as constructor:
            with self.assertRaises(ValueError):
                worker_runtime()
        constructor.assert_not_called()

    def test_unassigned_table_is_rejected_before_secret_or_model_access(self):
        no_table = self.settings.model_copy(update={"storage": self.settings.storage.model_copy(update={"table_name": None})})
        with patch.dict(os.environ, self.environ, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring(no_table)), \
                patch("boto3.Session") as constructor:
            with self.assertRaises(ValueError):
                worker_runtime()
        constructor.assert_not_called()

    def test_bad_secret_does_not_construct_the_model_or_public_storage(self):
        self.secrets.get_secret_value.return_value = {"SecretString": KEY + "\n"}
        with patch.dict(os.environ, self.environ, clear=True), \
                patch(RUNTIME + ".ServingConfigurationManager", return_value=self.wiring()), \
                patch("boto3.Session", return_value=self.session), \
                patch(RUNTIME + ".build_worker_pipeline") as build:
            with self.assertRaises(ValueError) as caught:
                worker_runtime()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual([call.args[0] for call in self.session.client.call_args_list], ["secretsmanager"])
        build.assert_not_called()

    def test_worker_import_is_offline_and_does_not_load_ml(self):
        code = ("import sys; import src.Sentiment_analysis.config.youtube; "
                "import src.Sentiment_analysis.workers.analyze; "
                "assert not any(k.split('.')[0] in {'boto3','numpy','sklearn','mlflow','dvc','pandas'} for k in sys.modules)")
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
