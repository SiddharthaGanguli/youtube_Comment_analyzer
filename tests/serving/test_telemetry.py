"""Privacy, EMF shape and committed ledger outcomes, without CloudWatch calls."""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.Sentiment_analysis.repositories.jobs import JobError
from src.Sentiment_analysis.services.telemetry import Telemetry
from tests.serving.aws_fixtures import StorageFixture


class TelemetryTests(unittest.TestCase):
    def test_emf_has_fixed_dimensions_and_no_identity_dimensions(self):
        records = []
        meter = Telemetry("development", "api", sink=records.append, clock_ms=lambda: 1000)
        self.assertTrue(meter.emit("api_request", {"RequestCount": 1, "RequestLatencyMillis": 12.5},
                                   request_id=uuid4(), route="GET /v1/analyses/{analysis_id}", status_code=200))
        record = records[0]
        self.assertEqual(record["_aws"]["Timestamp"], 1000)
        metric = record["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(metric["Namespace"], "CommentLens/Serving")
        self.assertEqual(metric["Dimensions"], [["Environment", "Service"]])
        self.assertEqual(metric["Metrics"], [{"Name": "RequestCount", "Unit": "Count"},
                                            {"Name": "RequestLatencyMillis", "Unit": "Milliseconds"}])

    def test_rejects_sensitive_fields_codes_dimensions_and_invalid_metric_values(self):
        records = []
        meter = Telemetry("development", "jobs", sink=records.append)
        for fields in [{"owner": "private-user"}, {"comment_text": "private text"}, {"author": "Private Name"},
                       {"token": "private-token"}, {"error_code": "private-secret"}, {"route": "/private/path"},
                       {"request_id": "private-input"}]:
            self.assertFalse(meter.emit("quota_spent", {"YouTubeAttemptUnits": 1}, **fields))
        for metrics in [{"user_email": 1}, {"YouTubeAttemptUnits": True}, {"YouTubeAttemptUnits": 1.5},
                        {"RequestLatencyMillis": float("nan")}, {"RequestLatencyMillis": float("inf")},
                        {"RequestCount": -1}, {}]:
            self.assertFalse(meter.emit("quota_spent", metrics))
        self.assertEqual(records, [])
        with self.assertRaises(ValueError):
            Telemetry("private-user", "api")

    def test_sink_failure_is_nonfatal(self):
        def unavailable(document):
            raise OSError("private dependency message")
        self.assertFalse(Telemetry("development", "api", sink=unavailable).emit("api_request", {"RequestCount": 1}))


class LedgerTelemetryTests(StorageFixture, unittest.TestCase):
    def test_spending_and_terminal_metrics_follow_successful_transactions_once(self):
        admission = self.admit(owner="private-person@example.test")
        leased = self.claim(admission.work)
        # Heartbeats are not provider allowance spending.
        self.jobs.heartbeat(leased.work_id, leased.lease_token)
        self.assertEqual([r for r in self.telemetry_records if r["event"] == "quota_spent"], [])
        self.jobs.spend_attempt(leased.work_id, leased.lease_token)
        self.assertEqual(sum(r.get("YouTubeAttemptUnits", 0) for r in self.telemetry_records), 1)
        from src.Sentiment_analysis.entity.analysis import Failure
        self.jobs.finish(leased.work_id, leased.lease_token, state="FAILED",
                         failure=Failure(code="VIDEO_NOT_FOUND", message="Safe fixed message", retryable=False))
        self.assertEqual(sum(r.get("JobFailedCount", 0) for r in self.telemetry_records), 1)
        self.assertIsNone(self.claim(admission.work))
        self.assertEqual(sum(r.get("JobFailedCount", 0) for r in self.telemetry_records), 1)
        self.assertNotIn("private-person", json.dumps(self.telemetry_records))

    def test_denied_quota_emits_a_counter_without_creating_work(self):
        budget = self.config.youtube.model_copy(update={"project_daily_quota_units": 1, "working_quota_fraction": 1.0})
        self.jobs.settings = self.config.model_copy(update={"youtube": budget})
        with self.assertRaises(JobError):
            self.admit()
        self.assertEqual(sum(r.get("QuotaDeniedCount", 0) for r in self.telemetry_records), 1)
        self.assertIsNone(self.store.get("LIMIT#GLOBAL#ACTIVE"))

    def test_cache_join_replay_and_logging_failure_preserve_admission(self):
        key = uuid4()
        first = self.admit(key=key)
        self.admit(key=key)
        self.admit(owner="bob")
        self.assertEqual(sum(r.get("NewWorkCount", 0) for r in self.telemetry_records), 1)
        self.assertEqual(sum(r.get("ReplayCount", 0) for r in self.telemetry_records), 1)
        self.assertEqual(sum(r.get("JoinedWorkCount", 0) for r in self.telemetry_records), 1)
        self.complete(first.work)
        self.admit(owner="carol")
        self.assertEqual(sum(r.get("CacheHitCount", 0) for r in self.telemetry_records), 1)
        self.jobs.telemetry = Telemetry("development", "jobs", sink=lambda _: (_ for _ in ()).throw(OSError("sink failure")))
        self.assertTrue(self.admit(owner="dave").request.cache_hit)
