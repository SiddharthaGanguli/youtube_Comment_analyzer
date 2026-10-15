"""CloudFormation identifiers must not weaken the packaged serving contract."""

import json
import unittest

from src.Sentiment_analysis.config.deployment import runtime_settings


class DeploymentSettingsTests(unittest.TestCase):
    def test_defaults_remain_offline_and_live_disabled(self):
        config = runtime_settings({})
        self.assertIsNone(config.storage.table_name)
        self.assertFalse(config.youtube.live_enabled)

    def test_valid_resource_overrides_preserve_packaged_limits_and_paths(self):
        original = runtime_settings({})
        config = runtime_settings({"SERVING_RUNTIME_OVERRIDES_JSON": json.dumps({
            "storage": {"table_name": "comment-lens-development-jobs", "bucket": "comment-lens-results"},
            "model": {"release_id": "sr_" + "a" * 64, "code_revision": "b" * 40},
            "api": {"allowed_origins": ["https://siddharthaganguli.github.io"], "reserved_concurrency": 10},
        })})
        self.assertEqual(config.storage.table_name, "comment-lens-development-jobs")
        self.assertEqual(config.model.model_file, original.model.model_file)
        self.assertEqual(config.youtube.maximum_comments, original.youtube.maximum_comments)
        self.assertFalse(config.youtube.live_enabled)

    def test_invalid_unknown_secret_and_duplicate_values_are_sanitized(self):
        cases = [
            '{"auth":{"client_secret":"private-test-value"}}',
            '{"youtube":{"api_key":"private-test-value"}}',
            '{"environment":"production"}',
            '{"api":{"allowed_origins":["https://*.example.com"]}}',
            '{"api":{"allowed_origins":[],"allowed_origins":[]}}',
            '{"api":{"timeout_seconds":999}}',
            '{"youtube":{"live_enabled":true}}',
            '{"storage":{"table_name":NaN}}',
            '{"model":{"model_file":"../../outside"}}',
            '[]', '{broken', ' ' * 16385,
        ]
        for raw in cases:
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError) as raised:
                runtime_settings({"SERVING_RUNTIME_OVERRIDES_JSON": raw})
            self.assertNotIn("private-test-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
