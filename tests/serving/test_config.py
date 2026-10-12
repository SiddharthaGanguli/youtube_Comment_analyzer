import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from src.Sentiment_analysis.config.serving import ServingConfigurationManager

ROOT = Path(__file__).resolve().parents[2]


class ServingConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "serving.yaml"
        self.data = yaml.safe_load((ROOT / "config/serving.yaml").read_text(encoding="utf-8"))

    def tearDown(self):
        self.directory.cleanup()

    def load(self, data=None):
        self.path.write_text(yaml.safe_dump(data or self.data), encoding="utf-8")
        return ServingConfigurationManager(self.path, project_root=self.root).get_serving_config()

    def test_development_load_is_typed_and_offline(self):
        config = self.load()
        self.assertEqual(config.target_active_users, 1000)
        self.assertEqual(config.youtube.working_daily_quota_units, 8000)
        self.assertEqual(config.model.model_file, self.root / "artifacts/model_training/sentiment_model.joblib")
        self.assertFalse(config.model.model_file.exists())
        self.assertFalse(config.youtube.live_enabled)
        self.assertIsNone(config.model.release_id)
        self.assertEqual(config.client.poll_schedule_seconds, (2, 5, 10))

    def test_import_does_not_load_pipeline_or_aws_dependencies(self):
        code = "import sys; from src.Sentiment_analysis.config.serving import ServingConfigurationManager; ServingConfigurationManager().get_serving_config(); print([name for name in ('numpy','pandas','sklearn','mlflow','boto3') if name in sys.modules])"
        completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_documented_example_is_runnable_and_matches_current_settings(self):
        runtime = ServingConfigurationManager().get_serving_config()
        example = ServingConfigurationManager("docs/serving-settings.example.yaml").get_serving_config()
        self.assertEqual(example, runtime)

    def test_bad_limits_and_types_are_rejected(self):
        changes = [
            ("api", "timeout_seconds", "10"), ("worker", "batch_size", True),
            ("worker", "review_confidence_threshold", float("nan")),
            ("worker", "lease_seconds", 180), ("worker", "soft_deadline_seconds", 180),
            ("worker", "sum_of_release_mapping_maximum_concurrency", 11),
            ("queue", "visibility_timeout_seconds", 1079), ("queue", "job_deadline_seconds", 100),
            ("queue", "event_batch_size", True), ("youtube", "page_size", 101),
            ("youtube", "maximum_comments", 1001), ("youtube", "full_sample_reserved_units", 11),
            ("youtube", "include_replies", 0), ("worker", "restore_with_dvc", True),
            ("cache", "freshness_seconds", 86400), ("cache", "download_url_seconds", 301),
            ("client", "poll_schedule_seconds", [10, 5, 2]),
            ("admission", "user_reads_per_minute", 1),
        ]
        for section, key, value in changes:
            data = copy.deepcopy(self.data)
            data[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                self.load(data)

    def test_live_enable_requires_all_declared_prerequisites(self):
        data = copy.deepcopy(self.data)
        data["youtube"]["live_enabled"] = True
        with self.assertRaisesRegex(ValueError, "Live analysis requires"):
            self.load(data)
        data["youtube"].update(analytics_use_approved=True, approval_reference="fixture-approval",
                               api_key_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-key-123456",
                               project_quota_verified=True)
        self.assertTrue(self.load(data).youtube.live_enabled)
        for key in ["analytics_use_approved", "approval_reference", "api_key_secret_arn", "project_quota_verified"]:
            missing = copy.deepcopy(data)
            missing["youtube"][key] = False if isinstance(missing["youtube"][key], bool) else None
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.load(missing)

    def test_production_readiness_and_region_binding(self):
        data = copy.deepcopy(self.data)
        data.update(environment="production", monthly_budget_usd=20)
        data["storage"].update(serving_prefix="serving/production", table_name="fixture-jobs")
        data["model"].update(release_id="sr_" + "1" * 64, code_revision="2" * 40)
        data["auth"].update(cognito_issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Fixture",
                            public_client_id="fixture123", extension_id="a" * 32)
        data["api"]["allowed_origins"] = ["chrome-extension://" + "a" * 32]
        data["youtube"].update(analytics_use_approved=True, approval_reference="fixture-approval",
                               api_key_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-key-123456",
                               project_quota_verified=True)
        self.load(data).validate_for_deployment()
        for section, key in [(None, "monthly_budget_usd"), ("model", "release_id"),
                             ("auth", "public_client_id"), ("storage", "table_name")]:
            missing = copy.deepcopy(data)
            (missing if section is None else missing[section])[key] = None
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.load(missing)
        for section, key, value in [
            ("youtube", "api_key_secret_arn", "arn:aws:secretsmanager:eu-west-1:123456789012:secret:fixture"),
            ("auth", "cognito_issuer", "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_Fixture"),
            ("api", "allowed_origins", ["http://localhost:8765"]),
        ]:
            invalid = copy.deepcopy(data)
            invalid[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(invalid)

    def test_paths_origins_secrets_and_unknown_fields(self):
        changes = [
            ("model", "model_file", "../../outside.joblib"),
            ("model", "model_file", "..\\..\\outside.joblib"),
            ("model", "model_file", str(self.root / "absolute.joblib")),
            ("model", "model_file", "C:\\outside\\model.joblib"),
            ("model", "sha256", "not-a-checksum"),
            ("storage", "serving_prefix", "datasets/youtube-comments"),
            ("storage", "serving_prefix", "serving/development/../datasets"),
            ("api", "allowed_origins", ["https://*.example.com"]),
            ("api", "allowed_origins", ["https://example.com/path"]),
            ("api", "allowed_origins", ["http://example.com"]),
            ("auth", "client_secret", "fixture-secret-value"),
            ("youtube", "api_key", "fixture-secret-value"),
        ]
        for section, key, value in changes:
            data = copy.deepcopy(self.data)
            data[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError) as raised:
                self.load(data)
            self.assertNotIn("fixture-secret-value", str(raised.exception))
        self.data["api"]["allowed_origins"] = ["http://127.0.0.1:8765"]
        self.load()

    def test_yaml_duplicate_keys_tags_and_nonmapping_inputs(self):
        for raw in ["environment: development\nenvironment: production\n",
                    "api:\n  timeout_seconds: 10\n  timeout_seconds: 30\n",
                    "!!python/object:builtins.object {}", "[one, two]", "model: []"]:
            self.path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ServingConfigurationManager(self.path, project_root=self.root).get_serving_config()

    def test_cli_rejects_unready_deployment(self):
        completed = subprocess.run([sys.executable, "-m", "src.Sentiment_analysis.config.serving", "--production"],
                                   cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("explicit production configuration", completed.stderr)


if __name__ == "__main__":
    unittest.main()
