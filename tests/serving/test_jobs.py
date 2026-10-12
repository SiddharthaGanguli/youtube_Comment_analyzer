"""Exercise real adapter calls against Moto, with fake credentials and fixture data."""

import copy
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from moto import mock_aws

from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.config.serving_runtime import queue_registry
from src.Sentiment_analysis.entity.analysis import (
    AnalysisInput, AnalysisResult, Failure, ModelReference, Progress, Sampling, WorkMessage,
)
from src.Sentiment_analysis.integrations.objects import ObjectStore
from src.Sentiment_analysis.repositories.dynamo import Change, DynamoStore
from src.Sentiment_analysis.repositories.jobs import JobError, JobRepository, digest, quota_day
from src.Sentiment_analysis.components.job_delivery import Dispatcher
from src.Sentiment_analysis.pipeline.job_maintenance import Reconciler
from tests.serving.fixtures import MODEL, VIDEO_ID, result

ROOT = Path(__file__).resolve().parents[2]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws(config={"core": {"service_whitelist": ["dynamodb", "s3", "sqs"]}})
        self.aws.start()
        self.addCleanup(self.aws.stop)
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        self.ddb = session.client("dynamodb")
        self.ddb.create_table(
            TableName="serving-test", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": kind} for name, kind in (
                ("pk", "S"), ("due_pk", "S"), ("due_at", "N"), ("cleanup_pk", "S"),
                ("cleanup_at", "N"), ("owner_pk", "S"), ("created_at", "N"))],
            GlobalSecondaryIndexes=[{
                "IndexName": name, "KeySchema": [{"AttributeName": partition, "KeyType": "HASH"},
                                                   {"AttributeName": sort, "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            } for name, partition, sort in (
                ("DueIndex", "due_pk", "due_at"), ("CleanupIndex", "cleanup_pk", "cleanup_at"),
                ("OwnerIndex", "owner_pk", "created_at"))])
        self.config = ServingConfigurationManager(project_root=ROOT).get_serving_config()
        self.config = self.config.model_copy(update={"model": self.config.model.model_copy(update={
            "sha256": MODEL["sha256"], "preprocessing_sha256": MODEL["preprocessing_sha256"],
            "release_id": MODEL["release_id"]})})
        self.now = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
        self.store = DynamoStore(self.ddb, "serving-test")
        self.jobs = JobRepository(self.store, self.config, clock=lambda: self.now)
        self.s3 = session.client("s3", config=Config(signature_version="s3v4"))
        self.s3.create_bucket(Bucket=self.config.storage.bucket)
        self.objects = ObjectStore(self.s3, self.config, clock=lambda: self.now)
        self.sqs = session.client("sqs")
        self.queue = self.sqs.create_queue(QueueName="fixture-release")["QueueUrl"]
        self.dispatch = Dispatcher(self.jobs, self.sqs, lambda release: self.queue)
        self.reconcile = Reconciler(self.jobs, self.dispatch, self.objects)
        self.model = ModelReference.model_validate(MODEL)
        self.input = AnalysisInput(video_url=f"https://youtu.be/{VIDEO_ID}")

    def admit(self, owner="alice", key=None, input=None):
        return self.jobs.admit(owner, key or uuid4(), input or self.input, self.model)

    def message(self, work):
        return WorkMessage(work_id=work.work_id, release_id=work.model.release_id,
                           dispatch_generation=work.dispatch_generation)

    def claim(self, work):
        return self.jobs.claim(self.message(work))

    def error(self, code, callback):
        with self.assertRaises(JobError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def complete(self, work, *, empty=True):
        leased = self.claim(work)
        data = result(empty)
        data["expires_at"] = leased.expires_at.isoformat()
        data["sampling"]["requested_max_comments"] = leased.input.max_comments
        result_entity = AnalysisResult.model_validate(data)
        count = result_entity.summary.received
        progress = Progress(fetched=count, duplicates_removed=0, analyzed=count, skipped=0)
        leased = self.jobs.heartbeat(work.work_id, leased.lease_token, state="INFERENCING",
                                     progress=progress, sampling=result_entity.sampling)
        leased = self.jobs.heartbeat(work.work_id, leased.lease_token, state="SAVING")
        pointer = self.objects.write_result(leased, leased.lease_token, result_entity)
        self.jobs.finish(work.work_id, leased.lease_token, state=result_entity.state, result=pointer)
        return self.jobs.get_work(work.work_id)

    def test_conflicting_transaction_has_no_partial_writes(self):
        self.assertTrue(self.store.commit([Change("A", None, {"count": 1})]))
        self.assertFalse(self.store.commit([Change("A", None, {"count": 2}), Change("B", None, {"count": 3})]))
        self.assertEqual(self.store.get("A").data["count"], 1)
        self.assertIsNone(self.store.get("B"))

    def test_admission_is_atomic_and_replay_keeps_original_owned_id(self):
        key = uuid4()
        first = self.admit(key=key)
        replay = self.admit(key=key)
        self.assertTrue(replay.replay)
        self.assertEqual(first.request.analysis_id, replay.request.analysis_id)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 33)
        self.assertEqual(self.jobs.status("alice", first.request.analysis_id).state, "QUEUED")
        self.error("IDEMPOTENCY_CONFLICT", lambda: self.admit(key=key, input=AnalysisInput(video_url=self.input.video_url, max_comments=100)))

    def test_shared_work_has_private_request_ids_and_no_extra_reservation(self):
        alice = self.admit("alice")
        bob = self.admit("bob")
        self.assertEqual(alice.work.work_id, bob.work.work_id)
        self.assertNotEqual(alice.request.analysis_id, bob.request.analysis_id)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 33)
        self.error("NOT_FOUND", lambda: self.jobs.status("bob", alice.request.analysis_id))
        self.error("NOT_FOUND", lambda: self.jobs.status("alice", uuid4()))
        owned, cursor = self.store.owned(digest("alice"))
        self.assertEqual([row.data["analysis_id"] for row in owned], [str(alice.request.analysis_id)])
        self.assertIsNone(cursor)

    def test_losing_cache_claim_retries_and_joins_winner(self):
        original = self.store.commit
        raced = False

        def race(changes):
            nonlocal raced
            if not raced and any(c.key.startswith("CACHE#") for c in changes):
                raced = True
                self.admit("other")
            return original(changes)

        with patch.object(self.store, "commit", side_effect=race):
            loser = self.admit()
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)
        winner = self.store.get("CACHE#" + loser.work.cache_key)
        self.assertEqual(winner.data["work_id"], str(loser.work.work_id))

    def test_user_slots_are_reclaimed_on_terminal_or_owned_deletion(self):
        first = self.admit()
        self.admit()
        self.error("ACTIVE_JOB_LIMIT", lambda: self.admit())
        self.jobs.delete_owned("alice", first.request.analysis_id)
        self.jobs.delete_owned("alice", first.request.analysis_id)
        self.error("EXPIRED", lambda: self.jobs.status("alice", first.request.analysis_id))
        self.admit()
        self.now += timedelta(minutes=1)
        self.complete(first.work)
        admitted = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=100))
        self.assertEqual(admitted.work.state, "QUEUED")

    def test_global_capacity_and_quota_rejection_leave_no_owned_records(self):
        self.jobs.settings = self.config.model_copy(update={"admission": self.config.admission.model_copy(
            update={"maximum_nonterminal_work": 1})})
        self.admit()
        other = AnalysisInput(video_url=self.input.video_url, max_comments=100)
        self.error("SYSTEM_BUSY", lambda: self.admit("bob", input=other))
        self.assertEqual(self.store.owned(digest("bob"))[0], [])
        self.jobs.settings = self.config
        quota = self.store.get("QUOTA#2026-10-12")
        self.store.commit([Change(quota.key, quota.version, {"spent": 7967, "reserved": 33, "budget": 8000})])
        self.error("YOUTUBE_QUOTA_EXHAUSTED", lambda: self.admit("bob", input=other))
        self.assertEqual(self.store.owned(digest("bob"))[0], [])

    def test_minute_rate_limits_and_replay_do_not_consume_more(self):
        key = uuid4()
        admission = self.admit(key=key)
        self.complete(admission.work)
        self.admit()
        self.admit()
        self.error("RATE_LIMITED", lambda: self.admit())
        self.assertTrue(self.admit(key=key).replay)
        self.now += timedelta(minutes=1)
        self.admit()

    def test_cache_freshness_and_retention_do_not_depend_on_ttl(self):
        first = self.admit()
        self.complete(first.work)
        cached = self.admit("bob")
        self.assertTrue(cached.request.cache_hit)
        self.assertEqual(cached.work.work_id, first.work.work_id)
        self.now += timedelta(seconds=901)
        fresh = self.admit("charlie")
        self.assertNotEqual(fresh.work.work_id, first.work.work_id)
        self.assertEqual(self.jobs.status("alice", first.request.analysis_id).state, "SUCCEEDED")
        self.now = first.request.expires_at
        self.error("EXPIRED", lambda: self.jobs.status("alice", first.request.analysis_id))
        self.assertIsNotNone(self.store.get("REQUEST#" + str(first.request.analysis_id)))

    def test_duplicate_delivery_active_lease_and_wrong_release_do_no_work(self):
        first = self.admit()
        wrong = self.message(first.work).model_copy(update={"release_id": "sr_" + "f" * 64})
        self.assertIsNone(self.jobs.claim(wrong))
        lease = self.claim(first.work)
        self.assertEqual(lease.lease_token, 1)
        self.assertIsNone(self.claim(first.work))
        self.assertFalse(self.jobs.recover(lease.work_id))

    def test_expired_lease_requeues_preserves_checkpoint_and_fences_old_worker(self):
        first = self.admit()
        lease = self.claim(first.work)
        checkpoint = self.objects.write_checkpoint(lease, 1, {"page": 1, "next_page_token": "fixture"})
        self.jobs.heartbeat(lease.work_id, 1, checkpoint=checkpoint)
        original_deadline = lease.deadline_at
        self.now += timedelta(seconds=211)
        counts = self.reconcile.run()
        self.assertEqual(counts["recovered"], 1)
        queued = self.jobs.get_work(lease.work_id)
        self.assertEqual(queued.deadline_at, original_deadline)
        self.assertEqual(queued.dispatch_generation, 1)
        self.assertIsNone(self.claim(first.work))
        new = self.claim(queued)
        self.assertEqual(new.lease_token, 2)
        self.assertEqual(self.objects.read_checkpoint(new)["page"], 1)
        self.error("LEASE_LOST", lambda: self.jobs.heartbeat(lease.work_id, 1))
        self.error("LEASE_LOST", lambda: self.jobs.finish(lease.work_id, 1, state="FAILED", failure=Failure(
            code="FIXTURE", message="Fixture failure", retryable=False)))

    def test_explicit_retry_preserves_remaining_quota_and_deadline(self):
        admitted = self.admit()
        lease = self.claim(admitted.work)
        self.jobs.spend_attempt(lease.work_id, 1)
        self.assertTrue(self.jobs.recover(lease.work_id, token=1))
        new = self.claim(self.jobs.get_work(lease.work_id))
        self.assertEqual(new.quota_remaining, 32)
        self.assertEqual(new.deadline_at, lease.deadline_at)
        self.error("LEASE_LOST", lambda: self.jobs.spend_attempt(lease.work_id, 1))

    def test_terminal_release_happens_once_and_never_refunds_spent(self):
        first = self.admit()
        lease = self.claim(first.work)
        self.jobs.spend_attempt(lease.work_id, 1)
        failure = Failure(code="VIDEO_NOT_FOUND", message="Video unavailable", retryable=False)
        self.jobs.finish(lease.work_id, 1, state="FAILED", failure=failure)
        self.error("LEASE_LOST", lambda: self.jobs.finish(lease.work_id, 1, state="FAILED", failure=failure))
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data, {"budget": 8000, "spent": 1, "reserved": 0})
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)

    def test_overall_deadline_expires_queued_job_and_releases_once(self):
        first = self.admit()
        self.now = first.work.deadline_at
        self.assertIsNone(self.claim(first.work))
        counts = self.reconcile.run()
        self.assertEqual(counts["recovered"], 1)
        self.assertEqual(self.jobs.get_work(first.work.work_id).failure.code, "JOB_DEADLINE_EXCEEDED")
        self.assertFalse(self.jobs.expire(first.work.work_id))
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)

    def test_dispatch_crash_after_send_is_recovered_with_safe_duplicate(self):
        first = self.admit()
        key = f"OUTBOX#{first.work.work_id}#0"
        with patch.object(self.store, "commit", side_effect=RuntimeError("fixture crash")):
            with self.assertRaises(RuntimeError):
                self.dispatch.dispatch(key)
        self.assertFalse(self.store.get(key).data["sent"])
        self.now += timedelta(seconds=31)
        self.assertEqual(self.reconcile.run()["dispatched"], 1)
        messages = self.sqs.receive_message(QueueUrl=self.queue, MaxNumberOfMessages=10)["Messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["Body"], messages[1]["Body"])
        payload = json.loads(messages[0]["Body"])
        self.assertEqual(set(payload), {"work_id", "release_id", "dispatch_generation"})
        self.assertFalse(self.dispatch.dispatch(key))
        self.assertIsNotNone(self.jobs.claim(WorkMessage.model_validate(payload)))
        self.assertIsNone(self.jobs.claim(WorkMessage.model_validate(payload)))

    def test_queue_send_failure_leaves_outbox_pending_and_stream_partial_failure(self):
        first = self.admit()
        key = f"OUTBOX#{first.work.work_id}#0"
        event = {"Records": [{"eventName": "INSERT", "dynamodb": {
            "Keys": {"pk": {"S": key}}, "SequenceNumber": "42"}}]}
        with patch.object(self.sqs, "send_message", side_effect=RuntimeError("fixture outage")):
            self.assertEqual(self.dispatch.handle_stream(event), {"batchItemFailures": [{"itemIdentifier": "42"}]})
        self.assertFalse(self.store.get(key).data["sent"])
        self.assertEqual(self.dispatch.handle_stream(event), {"batchItemFailures": []})

    def test_private_results_checksum_and_download_expiry_ownership(self):
        first = self.admit()
        completed = self.complete(first.work, empty=False)
        headers = self.s3.head_object(Bucket=self.config.storage.bucket, Key=completed.result.key)
        self.assertEqual(headers["ServerSideEncryption"], "AES256")
        self.assertEqual(headers["Metadata"]["sha256"], completed.result.sha256)
        self.assertEqual(self.objects.read(completed, completed.result)["summary"]["analyzed"], 1)
        download = self.objects.download(self.jobs, "alice", first.request.analysis_id)
        self.assertEqual((download.expires_at - self.now).total_seconds(), 300)
        self.error("NOT_FOUND", lambda: self.objects.download(self.jobs, "bob", first.request.analysis_id))
        self.now = completed.expires_at - timedelta(seconds=10)
        download = self.objects.download(self.jobs, "alice", first.request.analysis_id)
        self.assertEqual((download.expires_at - self.now).total_seconds(), 10)
        self.jobs.delete_owned("alice", first.request.analysis_id)
        self.error("EXPIRED", lambda: self.objects.download(self.jobs, "alice", first.request.analysis_id))
        # The shared result stays available to another owner until retention expiry.
        self.assertIsNotNone(self.s3.head_object(Bucket=self.config.storage.bucket, Key=completed.result.key))

    def test_immutable_upload_and_corrupt_content_are_detected(self):
        lease = self.claim(self.admit().work)
        pointer = self.objects.write_checkpoint(lease, 1, {"page": 1})
        again = self.objects.write_checkpoint(lease, 1, {"page": 1})
        self.assertEqual(pointer, again)
        self.s3.put_object(Bucket=self.config.storage.bucket, Key=pointer.key, Body=b"corrupt")
        with self.assertRaises(ValueError):
            self.objects.read(lease, pointer)
        foreign = pointer.model_copy(update={"key": "datasets/youtube-comments/model.json"})
        with self.assertRaises(ValueError):
            self.objects.read(lease, foreign)

    def test_oversize_result_is_explicit_and_does_not_upload(self):
        lease = self.claim(self.admit().work)
        self.error("RESULT_TOO_LARGE", lambda: self.objects.write_checkpoint(lease, 1, {"text": "a" * 10485760}))
        self.assertNotIn("Contents", self.s3.list_objects_v2(Bucket=self.config.storage.bucket))

    def test_orphan_upload_cannot_publish_after_lease_loss_and_is_cleaned(self):
        first = self.admit()
        lease = self.claim(first.work)
        pointer = self.objects.write_checkpoint(lease, 1, {"page": 1})
        self.now += timedelta(seconds=211)
        self.jobs.recover(lease.work_id)
        newer = self.claim(self.jobs.get_work(lease.work_id))
        with self.assertRaises(ValueError):
            self.jobs.heartbeat(newer.work_id, 2, checkpoint=pointer)
        self.error("LEASE_LOST", lambda: self.jobs.heartbeat(lease.work_id, 1, checkpoint=pointer))
        self.now = first.work.expires_at + timedelta(seconds=3601)
        counts = self.reconcile.run()
        self.assertEqual(counts["errors"], 0)
        self.assertIsNone(self.jobs.get_work(lease.work_id))
        self.error("NOT_FOUND", lambda: self.jobs.status("alice", first.request.analysis_id))
        self.assertNotIn("Contents", self.s3.list_objects_v2(Bucket=self.config.storage.bucket))

    def test_cleanup_storage_failure_keeps_metadata_for_retry(self):
        first = self.admit()
        lease = self.claim(first.work)
        self.objects.write_checkpoint(lease, 1, {"page": 1})
        self.now = first.work.expires_at + timedelta(seconds=3601)
        with patch.object(self.objects, "delete_work_objects", side_effect=RuntimeError("fixture outage")):
            self.assertGreaterEqual(self.reconcile.run()["errors"], 1)
        self.assertIsNotNone(self.jobs.get_work(lease.work_id))
        self.assertEqual(self.reconcile.run()["errors"], 0)
        self.assertIsNone(self.jobs.get_work(lease.work_id))

    def test_quota_day_resets_at_pacific_midnight_including_dst(self):
        for before, after in (("2026-03-09T06:59:59+00:00", "2026-03-09T07:00:00+00:00"),
                              ("2026-11-02T07:59:59+00:00", "2026-11-02T08:00:00+00:00")):
            self.assertNotEqual(quota_day(datetime.fromisoformat(before)), quota_day(datetime.fromisoformat(after)))
        self.now = datetime(2026, 10, 13, 6, 59, 59, tzinfo=timezone.utc)
        lease = self.claim(self.admit().work)
        self.jobs.spend_attempt(lease.work_id, 1)
        self.now += timedelta(seconds=1)
        next_day = self.jobs.spend_attempt(lease.work_id, 1)
        self.assertEqual(next_day.quota_remaining, 31)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 0)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["spent"], 1)
        self.assertEqual(self.store.get("QUOTA#2026-10-13").data["reserved"], 31)
        self.assertEqual(self.store.get("QUOTA#2026-10-13").data["spent"], 1)

    def test_global_http_limit_and_exhausted_midnight_budget_do_not_debit(self):
        lease = self.claim(self.admit().work)
        for _ in range(5):
            self.jobs.spend_attempt(lease.work_id, 1)
        self.error("RATE_LIMITED", lambda: self.jobs.spend_attempt(lease.work_id, 1))
        self.assertEqual(self.jobs.get_work(lease.work_id).quota_remaining, 28)
        self.now = datetime(2026, 10, 13, 7, tzinfo=timezone.utc)
        # Extend this fixture's deadline/lease to isolate rollover from timeout handling.
        item = self.store.get("WORK#" + str(lease.work_id))
        data = copy.deepcopy(item.data)
        data.update(deadline_at=(self.now + timedelta(minutes=30)).isoformat(),
                    lease_until=(self.now + timedelta(seconds=210)).isoformat())
        self.store.commit([Change(item.key, item.version, data), Change("QUOTA#2026-10-13", None,
            {"budget": 8000, "spent": 8000, "reserved": 0})])
        self.error("YOUTUBE_QUOTA_EXHAUSTED", lambda: self.jobs.spend_attempt(lease.work_id, 1))
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 28)
        self.assertEqual(self.jobs.get_work(lease.work_id).quota_day, "2026-10-12")

    def test_read_rate_counter_is_owned_and_resets_without_ttl(self):
        for _ in range(30):
            self.jobs.charge_read("alice")
        self.error("RATE_LIMITED", lambda: self.jobs.charge_read("alice"))
        self.jobs.charge_read("bob")
        self.now += timedelta(minutes=1)
        self.jobs.charge_read("alice")

    def test_expired_active_slots_are_reclaimed_when_admitting_another_video(self):
        first = self.admit()
        self.admit()
        self.now = first.work.deadline_at + timedelta(seconds=1)
        other = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=100))
        self.assertNotEqual(other.work.work_id, first.work.work_id)
        self.assertEqual(self.jobs.get_work(first.work.work_id).state, "FAILED")
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)

    def test_cached_request_cannot_shorten_an_active_slots_cleanup_time(self):
        short = self.admit("bob")
        self.complete(short.work)
        self.now = short.work.expires_at - timedelta(seconds=10)
        # Pin a still-fresh result to isolate expiry-bound joins from freshness.
        item = self.store.get("WORK#" + str(short.work.work_id))
        data = dict(item.data, fresh_until=short.work.expires_at.isoformat())
        self.store.commit([Change(item.key, item.version, data)])
        active = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=100))
        self.admit()
        self.now = short.work.expires_at + timedelta(seconds=3601)
        # Do not invoke work recovery here: we are checking whether slot metadata
        # can disappear before the referenced request's expiry, regardless of state.
        index = self.store.due("cleanup", int(self.now.timestamp()), cleanup=True)
        slot_key = "LIMIT#SLOTS#" + digest("alice")
        self.assertNotIn(slot_key, [row.key for row in index])
        self.assertIsNotNone(self.store.get(slot_key))
        self.assertGreater(active.request.expires_at, self.now)

    def test_obsolete_outbox_is_removed_from_due_index(self):
        first = self.admit()
        lease = self.claim(first.work)
        self.jobs.recover(lease.work_id, token=1)
        self.now += timedelta(seconds=31)
        self.reconcile.run()
        old = self.store.get(f"OUTBOX#{lease.work_id}#0")
        self.assertTrue(old.data["discarded"])
        self.assertNotIn(old.key, [item.key for item in self.store.due("outbox", int(self.now.timestamp()))])

    def test_result_upload_before_crash_does_not_publish_or_release_capacity(self):
        first = self.admit()
        leased = self.claim(first.work)
        entity = AnalysisResult.model_validate(result(empty=True))
        leased = self.jobs.heartbeat(leased.work_id, 1, state="INFERENCING", sampling=entity.sampling)
        leased = self.jobs.heartbeat(leased.work_id, 1, state="SAVING")
        pointer = self.objects.write_result(leased, 1, entity)
        self.error("RESULT_NOT_READY", lambda: self.objects.download(self.jobs, "alice", first.request.analysis_id))
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)
        self.now += timedelta(seconds=211)
        self.jobs.recover(leased.work_id)
        self.claim(self.jobs.get_work(leased.work_id))
        self.error("LEASE_LOST", lambda: self.jobs.finish(leased.work_id, 1, state="SUCCEEDED", result=pointer))
        self.assertIsNone(self.jobs.get_work(leased.work_id).result)

    def test_atomic_terminal_release_retries_conflict_without_double_decrement(self):
        leased = self.claim(self.admit().work)
        original = self.store.commit
        conflicted = False

        def conflict(changes):
            nonlocal conflicted
            if not conflicted:
                conflicted = True
                # A concurrent job consumes another global slot/quota reservation.
                self.admit("bob", input=AnalysisInput(video_url=self.input.video_url, max_comments=100))
            return original(changes)

        with patch.object(self.store, "commit", side_effect=conflict):
            finished = self.jobs.finish(leased.work_id, 1, state="FAILED", failure=Failure(
                code="FIXTURE", message="Fixture failure", retryable=False))
        self.assertEqual(finished.quota_remaining, 0)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 6)

    def test_download_is_also_bounded_by_temporary_credentials(self):
        first = self.admit()
        self.complete(first.work)
        download = self.objects.download(self.jobs, "alice", first.request.analysis_id,
                                         credential_expires_at=self.now + timedelta(seconds=20))
        self.assertEqual((download.expires_at - self.now).total_seconds(), 20)

    def test_progress_and_sampling_changes_must_stay_consistent(self):
        lease = self.claim(self.admit().work)
        with self.assertRaises(ValueError):
            self.jobs.heartbeat(lease.work_id, 1, state="SAVING")
        progress = Progress(fetched=1, duplicates_removed=0, analyzed=0, skipped=0)
        self.jobs.heartbeat(lease.work_id, 1, progress=progress)
        with self.assertRaises(ValueError):
            self.jobs.heartbeat(lease.work_id, 1, progress=Progress(
                fetched=0, duplicates_removed=0, analyzed=0, skipped=0))

    def test_queue_registry_rejects_wrong_region_shared_queue_and_fifo(self):
        valid = {MODEL["release_id"]: self.queue}
        self.assertEqual(queue_registry(json.dumps(valid), "us-east-1"), valid)
        for invalid in ({}, {"bad-release": self.queue}, {MODEL["release_id"]: self.queue + ".fifo"},
                        {MODEL["release_id"]: self.queue.replace("us-east-1", "us-west-2")},
                        {MODEL["release_id"]: self.queue, "sr_" + "f" * 64: self.queue}):
            with self.assertRaises(ValueError):
                queue_registry(json.dumps(invalid), "us-east-1")

    def test_delivery_modules_import_without_model_libraries_or_network_calls(self):
        code = """
import sys
from src.Sentiment_analysis.workers.dispatch import handler
from src.Sentiment_analysis.workers.reconcile import handler
assert not {'sklearn', 'numpy', 'pandas', 'mlflow'} & set(sys.modules)
print('lightweight import passed')
"""
        process = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_objects_are_deleted_at_retention_expiry_before_metadata_purge(self):
        first = self.admit()
        work = self.complete(first.work)
        self.now = work.expires_at
        counts = self.reconcile.run()
        self.assertEqual(counts["errors"], 0)
        self.assertNotIn("Contents", self.s3.list_objects_v2(Bucket=self.config.storage.bucket))
        self.assertIsNotNone(self.jobs.get_work(work.work_id))
        self.error("EXPIRED", lambda: self.jobs.status("alice", first.request.analysis_id))

    def test_cleanup_removes_all_versions_and_preserves_dataset_prefix(self):
        self.s3.put_bucket_versioning(Bucket=self.config.storage.bucket, VersioningConfiguration={"Status": "Enabled"})
        lease = self.claim(self.admit().work)
        pointer = self.objects.write_checkpoint(lease, 1, {"page": 1})
        self.s3.put_object(Bucket=self.config.storage.bucket, Key=pointer.key, Body=b"another version")
        self.s3.delete_object(Bucket=self.config.storage.bucket, Key=pointer.key)
        dataset_key = "datasets/youtube-comments/fixture.json"
        self.s3.put_object(Bucket=self.config.storage.bucket, Key=dataset_key, Body=b"dataset fixture")
        self.now = lease.expires_at
        self.assertEqual(self.reconcile.run()["errors"], 0)
        remaining = self.s3.list_object_versions(Bucket=self.config.storage.bucket,
                                                  Prefix=f"{self.config.storage.serving_prefix}/work/{lease.work_id}/")
        self.assertNotIn("Versions", remaining)
        self.assertNotIn("DeleteMarkers", remaining)
        self.assertEqual(self.s3.get_object(Bucket=self.config.storage.bucket, Key=dataset_key)["Body"].read(), b"dataset fixture")

    def test_lost_admission_response_is_replayed_without_duplicate_work(self):
        key = uuid4()
        original = self.store.commit

        def response_lost(changes):
            self.assertTrue(original(changes))
            raise TimeoutError("Fixture: commit succeeded but response was lost")

        with patch.object(self.store, "commit", side_effect=response_lost):
            with self.assertRaises(TimeoutError):
                self.admit(key=key)
        replay = self.admit(key=key)
        self.assertTrue(replay.replay)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 33)

    def test_distinct_work_burst_stops_at_global_cap_but_shared_work_can_join(self):
        accepted = []
        rejected = 0
        for index in range(30):
            try:
                accepted.append(self.admit(f"user-{index}", input=AnalysisInput(
                    video_url=self.input.video_url, max_comments=index + 1)))
            except JobError as error:
                self.assertEqual(error.code, "SYSTEM_BUSY")
                rejected += 1
        self.assertEqual(len(accepted), 10)
        self.assertEqual(rejected, 20)
        joined = self.admit("viewer", input=accepted[0].work.input)
        self.assertEqual(joined.work.work_id, accepted[0].work.work_id)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 10)

    def test_changed_release_never_reuses_the_previous_release_cache(self):
        first = self.admit()
        self.complete(first.work)
        self.model = self.model.model_copy(update={"release_id": "sr_" + "f" * 64})
        self.jobs.settings = self.config.model_copy(update={"model": self.config.model.model_copy(
            update={"release_id": self.model.release_id})})
        newer = self.admit("bob")
        self.assertFalse(newer.request.cache_hit)
        self.assertNotEqual(newer.work.work_id, first.work.work_id)
        self.assertEqual(self.jobs.status("alice", first.request.analysis_id).model.release_id, MODEL["release_id"])

    def test_lambda_entry_points_use_the_same_tested_components(self):
        from src.Sentiment_analysis.workers import dispatch, reconcile
        first = self.admit()
        event = {"Records": [{"eventName": "INSERT", "dynamodb": {"Keys": {
            "pk": {"S": f"OUTBOX#{first.work.work_id}#0"}}, "SequenceNumber": "42"}}]}
        with patch.object(dispatch, "delivery_runtime", return_value=(self.dispatch, self.reconcile)):
            self.assertEqual(dispatch.handler(event, None), {"batchItemFailures": []})
        with patch.object(reconcile, "delivery_runtime", return_value=(self.dispatch, self.reconcile)):
            self.assertEqual(reconcile.handler({}, None)["errors"], 0)
        with patch.object(reconcile, "delivery_runtime", return_value=(self.dispatch, self.reconcile)), \
                patch.object(self.reconcile, "run", return_value={"errors": 1}):
            with self.assertRaises(RuntimeError):
                reconcile.handler({}, None)

    def test_claim_conflict_is_not_mistaken_for_an_acknowledgeable_duplicate(self):
        first = self.admit()
        original = self.store.commit
        count = 0

        def conflict_once(changes):
            nonlocal count
            count += 1
            return False if count == 1 else original(changes)

        with patch.object(self.store, "commit", side_effect=conflict_once):
            lease = self.claim(first.work)
        self.assertEqual(lease.lease_token, 1)
        self.jobs.recover(lease.work_id, token=1)
        queued = self.jobs.get_work(lease.work_id)
        with patch.object(self.store, "commit", return_value=False):
            self.error("SYSTEM_BUSY", lambda: self.claim(queued))
        self.assertEqual(self.jobs.get_work(lease.work_id).state, "QUEUED")
