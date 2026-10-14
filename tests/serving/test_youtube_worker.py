"""Live-provider workflow, exercised with synthetic responses and in-process AWS."""

import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from src.Sentiment_analysis.entity.analysis import AnalysisInput, AnalysisResult
from src.Sentiment_analysis.entity.api import release_fingerprint
from src.Sentiment_analysis.entity.worker import WorkerCheckpoint, WorkerIdentity
from src.Sentiment_analysis.integrations.fixture_comments import FixtureCommentProvider
from src.Sentiment_analysis.integrations.provider import ProviderError
from src.Sentiment_analysis.pipeline.analysis import AnalysisPipeline
from src.Sentiment_analysis.repositories.jobs import JobError
from tests.serving.aws_fixtures import ROOT, StorageFixture
from tests.serving.test_worker import CODE, SyntheticPredictor


class SyntheticLiveSource:
    """Exercise live resume rules without contacting YouTube."""
    is_fixture = False

    def __init__(self, fixture):
        self.fixture = fixture

    def video(self, video_id):
        return self.fixture.video(video_id)

    def comments(self, video_id, token, limit):
        return self.fixture.comments(video_id, token, limit)


class YouTubeWorkerTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.model = self.model.model_copy(update={"release_id": release_fingerprint(self.model, CODE)})
        self.config = self.config.model_copy(update={
            "model": self.config.model.model_copy(update={"release_id": self.model.release_id, "code_revision": CODE}),
            "youtube": self.config.youtube.model_copy(update={
                "live_enabled": True, "analytics_use_approved": True,
                "approval_reference": "synthetic offline test approval", "project_quota_verified": True,
                "api_key_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:youtube/test-ABC123",
                "global_http_attempts_per_second": 100})})
        self.jobs.settings = self.objects.settings = self.config
        data = json.loads((ROOT / "examples/worker-fixture.json").read_text(encoding="utf-8"))
        self.provider = SyntheticLiveSource(FixtureCommentProvider(self.config, data))
        self.predictor, self.elapsed = SyntheticPredictor(), 0
        self.pipeline = AnalysisPipeline(self.config, self.jobs, self.objects, self.provider, self.predictor,
            WorkerIdentity(code_revision=CODE, model=self.model), monotonic=lambda: self.elapsed, sleep=self.sleep)

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds

    def run_work(self, work):
        return self.pipeline.process(self.message(self.jobs.get_work(work.work_id)))

    def checkpoint(self, work):
        return WorkerCheckpoint.model_validate(self.objects.read_checkpoint(self.jobs.get_work(work.work_id)))

    def spent(self, work):
        return self.store.get("QUOTA#" + work.quota_day).data["spent"]

    def downloaded(self, work):
        current = self.jobs.get_work(work.work_id)
        return AnalysisResult.model_validate(self.objects.read(current, current.result))

    def test_retry_after_is_observed_within_invocation_and_charged_per_call(self):
        work = self.admit().work
        before = self.now
        times = []
        video = self.provider.video
        def response(video_id):
            times.append(self.now)
            if len(times) == 1:
                raise ProviderError("PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=120)
            return video(video_id)
        with patch.object(self.provider, "video", side_effect=response):
            self.assertTrue(self.run_work(work))
        self.assertEqual(times, [before, before + timedelta(seconds=120)])
        self.assertEqual(self.spent(work), 4)  # Failed metadata, metadata, two pages.
        self.assertIsNone(self.checkpoint(work).retry_not_before)
        self.assertEqual(self.jobs.get_work(work.work_id).state, "SUCCEEDED")

    def test_long_retry_after_is_checkpointed_and_delayed_without_early_lease(self):
        work = self.admit().work
        before = self.now
        with patch.object(self.provider, "video", side_effect=ProviderError(
                "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=600)) as calls:
            self.assertTrue(self.run_work(work))
            self.assertTrue(self.run_work(work))  # Even a premature duplicate cannot claim.
        self.assertEqual(calls.call_count, 1)
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.lease_token, current.dispatch_generation), ("QUEUED", 1, 1))
        self.assertEqual(self.checkpoint(work).retry_not_before, before + timedelta(seconds=600))
        self.assertEqual(self.spent(work), 1)
        self.now += timedelta(seconds=600)
        self.elapsed = 0
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).state, "SUCCEEDED")

    def test_retry_after_beyond_job_deadline_never_calls_again(self):
        work = self.admit().work
        with patch.object(self.provider, "video", side_effect=ProviderError(
                "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=86400)) as calls:
            self.assertTrue(self.run_work(work))
            self.now = work.deadline_at
            self.assertTrue(self.run_work(work))
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(self.spent(work), 1)
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "JOB_DEADLINE_EXCEEDED")

    def test_retry_after_survives_response_crossing_soft_deadline(self):
        work = self.admit().work
        def slow_response(video_id):
            self.elapsed = self.config.worker.soft_deadline_seconds + 1
            raise ProviderError("PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=600)
        with patch.object(self.provider, "video", side_effect=slow_response) as metadata:
            self.assertTrue(self.run_work(work))
            self.elapsed = 0
            self.assertTrue(self.run_work(work))
        metadata.assert_called_once()
        self.assertIsNone(self.checkpoint(work).retry_not_before)  # Save had no invocation headroom.
        outbox = self.store.get(f"OUTBOX#{work.work_id}#1")
        self.assertEqual(outbox.data["not_before"], int((self.now + timedelta(seconds=600)).timestamp()))
        self.assertEqual(self.jobs.get_work(work.work_id).lease_token, 1)
        self.now += timedelta(seconds=600)
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).state, "SUCCEEDED")

    def test_retry_after_survives_failed_checkpoint_upload(self):
        work = self.admit().work
        upload = self.objects.write_checkpoint
        def unavailable(leased, token, payload):
            if payload.get("retry_not_before"):
                raise OSError("synthetic upload outage")
            return upload(leased, token, payload)
        with patch.object(self.provider, "video", side_effect=ProviderError(
                "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=600)) as metadata, \
                patch.object(self.objects, "write_checkpoint", side_effect=unavailable):
            self.assertTrue(self.run_work(work))
            self.assertTrue(self.run_work(work))
        metadata.assert_called_once()
        self.assertEqual(self.jobs.get_work(work.work_id).lease_token, 1)
        self.assertEqual(self.spent(work), 1)
        self.assertEqual(self.store.get(f"OUTBOX#{work.work_id}#1").data["not_before"],
                         int((self.now + timedelta(seconds=600)).timestamp()))

    def test_oversized_retry_checkpoint_fails_without_repeated_recovery(self):
        work = self.admit().work
        upload = self.objects.write_checkpoint
        def bounded(leased, token, payload):
            if payload.get("retry_not_before"):
                raise JobError("RESULT_TOO_LARGE")
            return upload(leased, token, payload)
        with patch.object(self.provider, "video", side_effect=ProviderError(
                "PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=600)), \
                patch.object(self.objects, "write_checkpoint", side_effect=bounded):
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code, current.dispatch_generation),
                         ("FAILED", "RESULT_TOO_LARGE", 0))

    def stop_after_fetch(self, work):
        save = self.pipeline._save
        def stop(leased, checkpoint, *args):
            saved = save(leased, checkpoint, *args)
            if checkpoint.fetch_complete:
                raise SystemExit("synthetic hard crash")
            return saved
        with patch.object(self.pipeline, "_save", side_effect=stop), self.assertRaises(SystemExit):
            self.run_work(work)
        self.now += timedelta(seconds=self.config.worker.lease_seconds + 1)
        self.assertTrue(self.jobs.recover(work.work_id))
        self.elapsed = 0

    def test_resumed_complete_sample_requires_new_public_check_before_inference(self):
        work = self.admit().work
        self.stop_after_fetch(work)
        self.assertTrue(self.checkpoint(work).fetch_complete)
        with patch.object(self.provider, "video", side_effect=ProviderError("VIDEO_NOT_PUBLIC")) as metadata, \
                patch.object(self.provider, "comments") as pages:
            self.assertTrue(self.run_work(work))
        metadata.assert_called_once()
        pages.assert_not_called()
        self.assertEqual(self.predictor.calls, [])
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code, current.result), ("FAILED", "VIDEO_NOT_PUBLIC", None))
        self.assertEqual(self.spent(work), 4)

    def test_public_recheck_resumes_sample_without_refetch_and_preserves_sample_time(self):
        work = self.admit().work
        self.stop_after_fetch(work)
        fetched_at = self.checkpoint(work).fetched_at
        with patch.object(self.provider, "video", wraps=self.provider.video) as metadata, \
                patch.object(self.provider, "comments", side_effect=AssertionError("already fetched")):
            self.assertTrue(self.run_work(work))
        metadata.assert_called_once()
        self.assertEqual(self.downloaded(work).fetched_at, fetched_at)
        self.assertEqual(self.checkpoint(work).visibility_lease_token, 2)
        self.assertEqual(self.spent(work), 4)

    def test_visibility_counter_survives_crashes_and_never_uses_unverified_sample(self):
        work = self.admit().work
        self.stop_after_fetch(work)
        with patch.object(self.provider, "video", side_effect=SystemExit("crash after visibility call")) as calls:
            for _ in range(3):
                with self.assertRaises(SystemExit):
                    self.run_work(work)
                self.now += timedelta(seconds=self.config.worker.lease_seconds + 1)
                self.assertTrue(self.jobs.recover(work.work_id))
            self.assertTrue(self.run_work(work))
        self.assertEqual(calls.call_count, 3)
        self.assertEqual(self.predictor.calls, [])
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "FETCH_RETRIES_EXHAUSTED")

    def test_upstream_quota_fault_opens_circuit_and_can_finish_collected_sample(self):
        work = self.admit().work
        comments = self.provider.comments
        def response(video_id, token, size):
            if token is not None:
                raise ProviderError("YOUTUBE_QUOTA_EXHAUSTED", circuit_reason="quota")
            return comments(video_id, token, size)
        with patch.object(self.provider, "comments", side_effect=response) as calls:
            self.assertTrue(self.run_work(work))
        self.assertEqual(calls.call_count, 2)
        result = self.downloaded(work)
        self.assertEqual((result.state, result.partial_reason, result.summary.received), ("PARTIAL", "YOUTUBE_QUOTA_EXHAUSTED", 3))
        other = AnalysisInput(video_url=self.input.video_url, max_comments=999)
        self.error("YOUTUBE_QUOTA_EXHAUSTED", lambda: self.admit(input=other))
        self.assertEqual(self.spent(work), 3)

    def test_credentials_fault_blocks_other_accepted_work_without_another_call(self):
        first = self.admit().work
        second = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=999)).work
        with patch.object(self.provider, "video", side_effect=ProviderError(
                "PROVIDER_CREDENTIALS_INVALID", circuit_reason="credentials")) as metadata:
            self.assertTrue(self.run_work(first))
            self.assertTrue(self.run_work(second))
        self.assertEqual(metadata.call_count, 1)
        for work in (first, second):
            self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "PROVIDER_CREDENTIALS_INVALID")
        self.assertEqual(self.predictor.calls, [])
        self.assertEqual(self.spent(first), 1)

    def test_resumed_visibility_check_cannot_turn_quota_failure_into_partial_result(self):
        work = self.admit().work
        self.stop_after_fetch(work)
        with patch.object(self.provider, "video", side_effect=ProviderError(
                "YOUTUBE_QUOTA_EXHAUSTED", circuit_reason="quota")):
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code, current.result), ("FAILED", "YOUTUBE_QUOTA_EXHAUSTED", None))
        self.assertEqual(self.predictor.calls, [])
