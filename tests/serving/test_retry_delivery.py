"""Provider cooldowns survive Lambda exits without repeatedly consuming leases."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.Sentiment_analysis.entity.analysis import WorkMessage
from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.jobs import epoch
from tests.serving.aws_fixtures import StorageFixture


class RetryDeliveryTests(StorageFixture, unittest.TestCase):
    def delayed(self, seconds=120):
        work = self.claim(self.admit().work)
        not_before = self.now + timedelta(seconds=seconds)
        self.assertTrue(self.jobs.recover(work.work_id, token=work.lease_token, not_before=not_before))
        queued = self.jobs.get_work(work.work_id)
        key = f"OUTBOX#{work.work_id}#{queued.dispatch_generation}"
        return work, queued, key, not_before

    def stream_event(self, key):
        return {"Records": [{"eventName": "INSERT", "dynamodb": {
            "Keys": {"pk": {"S": key}}, "SequenceNumber": "fixture-sequence"}}]}

    def test_retry_is_durable_pending_outbox_without_an_active_lease(self):
        leased, queued, key, not_before = self.delayed()
        self.assertEqual(queued.state, "QUEUED")
        self.assertIsNone(queued.lease_until)
        self.assertEqual(queued.lease_token, leased.lease_token)
        self.assertEqual(queued.dispatch_generation, leased.dispatch_generation + 1)
        self.assertEqual(queued.deadline_at, leased.deadline_at)
        self.assertEqual(queued.quota_remaining, leased.quota_remaining)
        self.assertEqual(queued.checkpoint, leased.checkpoint)
        item = self.store.get(key)
        self.assertFalse(item.data["sent"])
        self.assertEqual(item.data["not_before"], epoch(not_before))
        self.assertEqual(set(item.data["message"]), {"work_id", "release_id", "dispatch_generation"})
        self.assertNotIn("not_before", queued.model_dump())
        due = self.store.due("outbox", epoch(not_before))
        self.assertIn(key, [row.key for row in due])
        self.assertNotIn(key, [row.key for row in self.store.due("outbox", epoch(not_before) - 1)])

    def test_stream_and_reconciler_leave_future_delivery_pending_without_sending(self):
        leased, queued, key, not_before = self.delayed(seconds=1200)
        with patch.object(self.sqs, "send_message", wraps=self.sqs.send_message) as send:
            self.assertFalse(self.dispatch.dispatch(key))
            self.assertEqual(self.dispatch.handle_stream(self.stream_event(key)), {"batchItemFailures": []})
            for offset in (31, 120, 300, 1199):
                self.now = leased.updated_at + timedelta(seconds=offset)
                self.assertEqual(self.reconcile.run()["dispatched"], 0)
                self.assertIsNone(self.jobs.claim(self.message(queued)))
            send.assert_not_called()
        self.assertFalse(self.store.get(key).data["sent"])
        self.assertEqual(self.jobs.get_work(leased.work_id).lease_token, 1)
        self.assertEqual(self.jobs.get_work(leased.work_id).dispatch_generation, 1)
        self.assertEqual(not_before, leased.updated_at + timedelta(seconds=1200))

    def test_due_delivery_sends_once_and_then_permits_the_next_lease(self):
        leased, queued, key, not_before = self.delayed(seconds=1200)
        self.now = not_before
        with patch.object(self.sqs, "send_message", wraps=self.sqs.send_message) as send:
            self.assertEqual(self.reconcile.run()["dispatched"], 1)
            self.assertFalse(self.dispatch.dispatch(key))
            send.assert_called_once()
            self.assertNotIn("DelaySeconds", send.call_args.kwargs)
        messages = self.sqs.receive_message(QueueUrl=self.queue, MaxNumberOfMessages=10)["Messages"]
        self.assertEqual(len(messages), 1)
        message = WorkMessage.model_validate(json.loads(messages[0]["Body"]))
        self.assertEqual(message.dispatch_generation, queued.dispatch_generation)
        claimed = self.jobs.claim(message)
        self.assertEqual(claimed.lease_token, leased.lease_token + 1)
        self.assertIsNone(self.jobs.claim(message))
        self.assertIsNone(self.jobs.claim(self.message(leased)))

    def test_delay_beyond_deadline_expires_work_without_delivery_and_releases_once(self):
        leased, _, key, requested = self.delayed(seconds=86400)
        self.assertGreater(requested, leased.deadline_at)
        self.assertEqual(self.store.get(key).data["not_before"], epoch(leased.deadline_at))
        self.now = leased.deadline_at
        with patch.object(self.sqs, "send_message") as send:
            counts = self.reconcile.run()
            self.assertEqual(counts["recovered"], 1)
            self.assertEqual(counts["dispatched"], 0)
            send.assert_not_called()
        failed = self.jobs.get_work(leased.work_id)
        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.failure.code, "JOB_DEADLINE_EXCEEDED")
        self.assertEqual(failed.quota_remaining, 0)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)
        self.assertEqual(self.store.get("QUOTA#" + leased.quota_day).data["reserved"], 0)
        self.assertTrue(self.store.get(key).data["discarded"])
        self.assertFalse(self.jobs.expire(leased.work_id))

    def test_fractional_eligibility_never_dispatches_or_claims_early(self):
        leased = self.claim(self.admit().work)
        not_before = self.now + timedelta(seconds=120, microseconds=1)
        self.assertTrue(self.jobs.recover(leased.work_id, token=leased.lease_token, not_before=not_before))
        queued = self.jobs.get_work(leased.work_id)
        key = f"OUTBOX#{leased.work_id}#1"
        self.assertEqual(self.store.get(key).data["not_before"], epoch(not_before) + 1)
        self.now = not_before - timedelta(microseconds=1)
        self.assertFalse(self.dispatch.dispatch(key))
        self.assertIsNone(self.claim(queued))
        self.now = not_before + timedelta(seconds=1)
        self.assertTrue(self.dispatch.dispatch(key))
        self.assertEqual(self.claim(queued).lease_token, 2)

    def test_aware_offset_is_normalized_and_expired_cooldown_allows_immediate_retry(self):
        leased = self.claim(self.admit().work)
        eligible = (self.now + timedelta(seconds=120)).astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertTrue(self.jobs.recover(leased.work_id, token=leased.lease_token, not_before=eligible))
        queued = self.jobs.get_work(leased.work_id)
        self.assertEqual(self.store.get(f"OUTBOX#{leased.work_id}#1").data["not_before"], epoch(eligible))
        self.now = eligible.astimezone(timezone.utc)
        next_lease = self.claim(queued)
        self.assertTrue(self.jobs.recover(next_lease.work_id, token=next_lease.lease_token,
                                        not_before=self.now - timedelta(seconds=1)))
        key = f"OUTBOX#{leased.work_id}#2"
        self.assertTrue(self.dispatch.dispatch(key))
        self.assertEqual(self.claim(self.jobs.get_work(leased.work_id)).lease_token, 3)

    def test_invalid_cooldown_timestamp_cannot_mutate_lease_or_create_outbox(self):
        leased = self.claim(self.admit().work)
        for value in (datetime(2026, 10, 12, 10), "2026-10-12T10:02:00Z", 123, True):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    self.jobs.recover(leased.work_id, token=leased.lease_token, not_before=value)
                self.assertEqual(self.jobs.get_work(leased.work_id), leased)
                self.assertIsNone(self.store.get(f"OUTBOX#{leased.work_id}#1"))

    def test_wrong_or_expired_token_cannot_schedule_retry(self):
        leased = self.claim(self.admit().work)
        not_before = self.now + timedelta(seconds=120)
        self.error("LEASE_LOST", lambda: self.jobs.recover(leased.work_id, token=99, not_before=not_before))
        self.assertEqual(self.jobs.get_work(leased.work_id), leased)
        self.now = leased.lease_until
        self.error("LEASE_LOST", lambda: self.jobs.recover(leased.work_id, token=leased.lease_token,
                                                        not_before=self.now + timedelta(seconds=120)))
        self.assertIsNone(self.store.get(f"OUTBOX#{leased.work_id}#1"))

    def test_duplicate_recovery_does_not_create_another_generation(self):
        leased, queued, key, not_before = self.delayed()
        self.assertFalse(self.jobs.recover(leased.work_id, token=leased.lease_token, not_before=not_before))
        self.assertEqual(self.jobs.get_work(leased.work_id), queued)
        self.assertFalse(self.store.get(key).data["sent"])
        self.assertIsNone(self.store.get(f"OUTBOX#{leased.work_id}#2"))

    def test_recovery_transaction_conflict_retries_without_half_schedule(self):
        leased = self.claim(self.admit().work)
        original = self.store.commit
        attempted = False

        def conflict_once(changes):
            nonlocal attempted
            if not attempted:
                attempted = True
                return False
            return original(changes)

        with patch.object(self.store, "commit", side_effect=conflict_once) as commit:
            self.assertTrue(self.jobs.recover(leased.work_id, token=leased.lease_token,
                                            not_before=self.now + timedelta(seconds=120)))
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(self.jobs.get_work(leased.work_id).dispatch_generation, 1)
        self.assertFalse(self.store.get(f"OUTBOX#{leased.work_id}#1").data["sent"])
        self.assertIsNone(self.store.get(f"OUTBOX#{leased.work_id}#2"))

    def test_claim_outbox_version_condition_prevents_cooldown_race(self):
        admission = self.admit()
        key = f"OUTBOX#{admission.work.work_id}#0"
        original = self.store.commit
        raced = False

        def defer_before_claim_commit(changes):
            nonlocal raced
            if not raced and any(change.key == key and change.check_only for change in changes):
                raced = True
                item = self.store.get(key)
                delayed = dict(item.data, not_before=epoch(self.now) + 120)
                original([Change(key, item.version, delayed, due_family="outbox", due_at=epoch(self.now) + 120)])
            return original(changes)

        with patch.object(self.store, "commit", side_effect=defer_before_claim_commit):
            self.assertIsNone(self.claim(admission.work))
        self.assertTrue(raced)
        self.assertEqual(self.jobs.get_work(admission.work.work_id).lease_token, 0)
        self.assertEqual(self.jobs.get_work(admission.work.work_id).state, "QUEUED")

    def test_due_send_failure_remains_pending_for_next_reconciliation(self):
        leased, _, key, not_before = self.delayed()
        self.now = not_before
        with patch.object(self.sqs, "send_message", side_effect=OSError("fixture queue outage")):
            self.assertEqual(self.reconcile.run()["errors"], 1)
        self.assertFalse(self.store.get(key).data["sent"])
        self.assertEqual(self.reconcile.run()["dispatched"], 1)
        self.assertEqual(self.jobs.get_work(leased.work_id).lease_token, 1)


if __name__ == "__main__":
    unittest.main()
