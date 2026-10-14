"""Project-wide provider failures, calendar resets and transaction races in Moto."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from src.Sentiment_analysis.entity.analysis import AnalysisInput
from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.jobs import digest, quota_day
from src.Sentiment_analysis.repositories.provider_circuit import KEY, next_quota_reset
from tests.serving.aws_fixtures import StorageFixture


class ProviderCircuitTests(StorageFixture, unittest.TestCase):
    def metric_total(self, name):
        return sum(record.get(name, 0) for record in self.telemetry_records)

    def test_quota_opens_once_and_expires_exactly_at_pacific_midnight_without_ttl(self):
        self.assertIsNone(self.jobs.circuit.assert_available())
        self.jobs.circuit.open("quota")
        opened = self.store.get(KEY)
        self.assertTrue(opened.data["open"])
        self.assertEqual(opened.data["reason"], "quota")
        self.assertEqual(opened.data["blocked_until"], int(datetime(2026, 10, 13, 7, tzinfo=timezone.utc).timestamp()))
        self.jobs.circuit.open("quota")
        self.assertEqual(self.store.get(KEY).version, opened.version)
        self.assertEqual(self.metric_total("ProviderCircuitOpenedCount"), 1)
        self.now = next_quota_reset(self.now) - timedelta(seconds=1)
        self.error("YOUTUBE_QUOTA_EXHAUSTED", self.jobs.circuit.assert_available)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.jobs.circuit.assert_available().version, opened.version)
        # The row still exists and is marked open; calendar expiry, not DynamoDB
        # removal, makes it usable again.
        self.assertTrue(self.store.get(KEY).data["open"])
        self.assertEqual(self.metric_total("ProviderCircuitBlockedCount"), 1)
        self.assertEqual(self.admit().work.quota_day, "2026-10-13")

    def test_quota_calendar_reset_respects_spring_and_autumn_dst(self):
        cases = [(datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc),
                  datetime(2026, 3, 9, 7, tzinfo=timezone.utc)),
                 (datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc),
                  datetime(2026, 11, 2, 8, tzinfo=timezone.utc))]
        for now, reset in cases:
            with self.subTest(day=now.date()):
                self.now = now
                self.assertEqual(next_quota_reset(now), reset)
                self.jobs.circuit.open("quota")
                self.assertEqual(self.store.get(KEY).data["blocked_until"], int(reset.timestamp()))
                self.now = reset - timedelta(seconds=1)
                self.error("YOUTUBE_QUOTA_EXHAUSTED", self.jobs.circuit.assert_available)
                self.now = reset
                self.jobs.circuit.assert_available()

    def test_credentials_persist_across_calendar_days_until_operator_repairs_expected_version(self):
        self.jobs.circuit.open("credentials")
        opened = self.store.get(KEY)
        self.assertIsNone(opened.data["blocked_until"])
        self.now += timedelta(days=14)
        self.error("DEPENDENCY_UNAVAILABLE", self.jobs.circuit.assert_available)
        self.jobs.circuit.close_after_credentials_repaired(expected_version=opened.version)
        closed = self.store.get(KEY)
        self.assertFalse(closed.data["open"])
        self.assertEqual(closed.version, opened.version + 1)
        self.assertEqual(self.jobs.circuit.assert_available().version, closed.version)
        self.assertEqual(self.admit().work.state, "QUEUED")

    def test_credential_failure_overrides_quota_and_quota_cannot_override_credentials(self):
        self.jobs.circuit.open("quota")
        self.jobs.circuit.open("credentials")
        credentials = self.store.get(KEY)
        self.assertEqual(credentials.data["reason"], "credentials")
        self.now += timedelta(days=1)
        self.jobs.circuit.open("quota")
        self.jobs.circuit.open("credentials")
        self.assertEqual(self.store.get(KEY), credentials)
        self.error("DEPENDENCY_UNAVAILABLE", self.jobs.circuit.assert_available)
        self.assertEqual(self.metric_total("ProviderCircuitOpenedCount"), 2)

    def test_stale_operator_version_cannot_close_reopened_credential_circuit(self):
        self.jobs.circuit.open("credentials")
        initial = self.store.get(KEY)
        self.jobs.circuit.close_after_credentials_repaired(expected_version=initial.version)
        self.jobs.circuit.open("credentials")
        reopened = self.store.get(KEY)
        with self.assertRaises(ValueError):
            self.jobs.circuit.close_after_credentials_repaired(expected_version=initial.version)
        self.assertEqual(self.store.get(KEY), reopened)
        self.error("DEPENDENCY_UNAVAILABLE", self.jobs.circuit.assert_available)

    def test_operator_close_rejects_quota_or_missing_state(self):
        with self.assertRaises(ValueError):
            self.jobs.circuit.close_after_credentials_repaired(expected_version=1)
        self.jobs.circuit.open("quota")
        opened = self.store.get(KEY)
        with self.assertRaises(ValueError):
            self.jobs.circuit.close_after_credentials_repaired(expected_version=opened.version)
        self.assertEqual(self.store.get(KEY), opened)

    def test_operator_close_loses_race_without_erasing_newer_credential_state(self):
        self.jobs.circuit.open("credentials")
        opened = self.store.get(KEY)
        original = self.store.commit
        raced = False

        def concurrent_fault(changes):
            nonlocal raced
            if not raced:
                raced = True
                original([Change(KEY, opened.version, {**opened.data, "opened_at": opened.data["opened_at"] + 1})])
            return original(changes)

        with patch.object(self.store, "commit", side_effect=concurrent_fault):
            self.error("SYSTEM_BUSY", lambda: self.jobs.circuit.close_after_credentials_repaired(
                expected_version=opened.version))
        current = self.store.get(KEY)
        self.assertTrue(current.data["open"])
        self.assertEqual(current.version, opened.version + 1)
        self.error("DEPENDENCY_UNAVAILABLE", self.jobs.circuit.assert_available)

    def test_open_retries_one_conflict_and_emits_only_after_commit(self):
        original = self.store.commit
        first = True

        def conflict_once(changes):
            nonlocal first
            if first:
                first = False
                return False
            return original(changes)

        with patch.object(self.store, "commit", side_effect=conflict_once) as commit:
            self.jobs.circuit.open("quota")
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(self.store.get(KEY).version, 1)
        self.assertEqual(self.metric_total("ProviderCircuitOpenedCount"), 1)

    def test_open_conflicts_are_bounded_and_unknown_reasons_do_not_write(self):
        with patch.object(self.store, "commit", return_value=False) as commit:
            self.error("SYSTEM_BUSY", lambda: self.jobs.circuit.open("quota"))
        self.assertEqual(commit.call_count, 8)
        self.assertIsNone(self.store.get(KEY))
        self.assertEqual(self.metric_total("ProviderCircuitOpenedCount"), 0)
        for reason in (None, "transient", "private input"):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    self.jobs.circuit.open(reason)
        self.assertIsNone(self.store.get(KEY))

    def existing_requests_remain_usable(self, reason, code):
        key = uuid4()
        first = self.admit(key=key)
        initial_ledger = dict(self.store.get("QUOTA#" + quota_day(self.now)).data)
        self.jobs.circuit.open(reason)
        joined = self.admit("bob")
        replayed = self.admit(key=key)
        self.assertEqual(joined.work.work_id, first.work.work_id)
        self.assertNotEqual(joined.request.analysis_id, first.request.analysis_id)
        self.assertTrue(replayed.replay)
        self.assertEqual(replayed.request.analysis_id, first.request.analysis_id)
        self.assertEqual(self.jobs.status("bob", joined.request.analysis_id).state, "QUEUED")
        self.assertEqual(self.store.get("QUOTA#" + quota_day(self.now)).data, initial_ledger)
        different = AnalysisInput(video_url=self.input.video_url, max_comments=100)
        self.error(code, lambda: self.admit("dave", input=different))
        self.assertEqual(self.store.owned(digest("dave"))[0], [])
        # This fixture publishes an already obtained result without another HTTP
        # attempt. Serving it must remain possible while the provider is blocked.
        self.complete(first.work)
        cached = self.admit("carol")
        self.assertTrue(cached.request.cache_hit)
        self.assertEqual(cached.work.work_id, first.work.work_id)
        self.assertEqual(self.jobs.status("carol", cached.request.analysis_id).state, "SUCCEEDED")
        self.assertEqual(self.metric_total("NewWorkCount"), 1)
        self.assertEqual(self.metric_total("JoinedWorkCount"), 1)
        self.assertEqual(self.metric_total("ReplayCount"), 1)
        self.assertEqual(self.metric_total("CacheHitCount"), 1)

    def test_quota_circuit_allows_cache_join_replay_and_owned_status(self):
        self.existing_requests_remain_usable("quota", "YOUTUBE_QUOTA_EXHAUSTED")

    def test_credentials_circuit_allows_cache_join_replay_and_owned_status(self):
        self.existing_requests_remain_usable("credentials", "DEPENDENCY_UNAVAILABLE")

    def test_expired_fresh_cache_needs_new_work_and_is_blocked(self):
        admitted = self.admit()
        self.complete(admitted.work)
        self.jobs.circuit.open("quota")
        self.now += timedelta(seconds=self.config.cache.freshness_seconds + 1)
        self.error("YOUTUBE_QUOTA_EXHAUSTED", lambda: self.admit("bob"))
        self.assertEqual(self.jobs.status("alice", admitted.request.analysis_id).state, "SUCCEEDED")
        self.assertEqual(self.store.owned(digest("bob"))[0], [])

    def test_open_circuit_prevents_spending_without_changing_ledger_work_or_limiter(self):
        admitted = self.admit()
        leased = self.claim(admitted.work)
        quota_key = "QUOTA#" + quota_day(self.now)
        ledger = self.store.get(quota_key)
        for reason, code in (("quota", "YOUTUBE_QUOTA_EXHAUSTED"), ("credentials", "DEPENDENCY_UNAVAILABLE")):
            with self.subTest(reason=reason):
                self.jobs.circuit.open(reason)
                self.error(code, lambda: self.jobs.spend_attempt(leased.work_id, leased.lease_token))
                self.assertEqual(self.store.get(quota_key), ledger)
                self.assertEqual(self.jobs.get_work(leased.work_id).quota_remaining, leased.quota_remaining)
                self.assertIsNone(self.store.get("LIMIT#HTTP#" + str(int(self.now.timestamp()))))
                self.assertEqual(self.metric_total("YouTubeAttemptUnits"), 0)

    def test_circuit_open_racing_new_admission_prevents_partial_ledger_and_owned_writes(self):
        original = self.store.commit
        raced = False

        def fault_before_admission_commit(changes):
            nonlocal raced
            if not raced and any(change.key == KEY and change.check_only for change in changes):
                raced = True
                self.jobs.circuit.open("quota")
            return original(changes)

        with patch.object(self.store, "commit", side_effect=fault_before_admission_commit):
            self.error("YOUTUBE_QUOTA_EXHAUSTED", lambda: self.admit())
        self.assertTrue(raced)
        self.assertIsNone(self.store.get("QUOTA#" + quota_day(self.now)))
        self.assertIsNone(self.store.get("LIMIT#GLOBAL#ACTIVE"))
        self.assertEqual(self.store.owned(digest("alice"))[0], [])
        self.assertEqual(self.metric_total("NewWorkCount"), 0)

    def test_circuit_open_racing_spend_prevents_quota_charge(self):
        admitted = self.admit()
        leased = self.claim(admitted.work)
        ledger_key = "QUOTA#" + quota_day(self.now)
        ledger = self.store.get(ledger_key)
        original = self.store.commit
        raced = False

        def fault_before_spend_commit(changes):
            nonlocal raced
            if not raced and any(change.key == KEY and change.check_only for change in changes):
                raced = True
                self.jobs.circuit.open("credentials")
            return original(changes)

        with patch.object(self.store, "commit", side_effect=fault_before_spend_commit):
            self.error("DEPENDENCY_UNAVAILABLE", lambda: self.jobs.spend_attempt(leased.work_id, leased.lease_token))
        self.assertTrue(raced)
        self.assertEqual(self.store.get(ledger_key), ledger)
        self.assertEqual(self.jobs.get_work(leased.work_id).quota_remaining, leased.quota_remaining)
        self.assertIsNone(self.store.get("LIMIT#HTTP#" + str(int(self.now.timestamp()))))
        self.assertEqual(self.metric_total("YouTubeAttemptUnits"), 0)


if __name__ == "__main__":
    unittest.main()
