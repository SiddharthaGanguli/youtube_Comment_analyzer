"""Worker protocol tests use synthetic predictions and in-process AWS only."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.Sentiment_analysis.config.worker import WorkerConfigurationManager
from src.Sentiment_analysis.config.worker_runtime import worker_runtime
from src.Sentiment_analysis.entity.analysis import AnalysisInput, AnalysisResult, ModelReference
from src.Sentiment_analysis.entity.api import release_fingerprint
from src.Sentiment_analysis.entity.worker import WorkerCheckpoint, WorkerIdentity
from src.Sentiment_analysis.integrations.fixture_comments import FixtureCommentProvider, ProviderError
from src.Sentiment_analysis.pipeline.analysis import AnalysisPipeline
from src.Sentiment_analysis.repositories.jobs import JobError
from src.Sentiment_analysis.workers.analyze import handler
from tests.serving.aws_fixtures import ROOT, StorageFixture

CODE = "4" * 40


class SyntheticPredictor:
    def __init__(self):
        self.calls = []

    def predict(self, texts):
        self.calls.append(list(texts))
        rows = []
        for index, text in enumerate(texts):
            normalized = (text or "").strip()
            skipped = "empty_text" if not normalized else "no_known_features" if normalized == "unknown" else None
            rows.append({"index": index, "text": text, "normalized_text": normalized, "status": "skipped" if skipped else "analyzed",
                         "sentiment": None if skipped else "Positive", "sentiment_label": None if skipped else 2,
                         "confidence": None if skipped else .8,
                         "class_probabilities": None if skipped else {"Negative": .1, "Neutral": .1, "Positive": .8},
                         "needs_review": False, "skipped_reason": skipped})
        return {"comments": rows}


class WorkerTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        bound = self.model.model_copy(update={"release_id": release_fingerprint(self.model, CODE)})
        self.config = self.config.model_copy(update={
            "model": self.config.model.model_copy(update={"release_id": bound.release_id, "code_revision": CODE}),
            "worker": self.config.worker.model_copy(update={"batch_size": 2}),
            "youtube": self.config.youtube.model_copy(update={"global_http_attempts_per_second": 100})})
        self.jobs.settings, self.objects.settings, self.model = self.config, self.config, bound
        self.identity = WorkerIdentity(code_revision=CODE, model=self.model)
        self.data = json.loads((ROOT / "examples/worker-fixture.json").read_text(encoding="utf-8"))
        self.predictor = SyntheticPredictor()
        self.elapsed = 0
        self.provider = FixtureCommentProvider(self.config, self.data)
        self.pipeline = self.make_pipeline()

    def make_pipeline(self, provider=None, predictor=None):
        return AnalysisPipeline(self.config, self.jobs, self.objects, provider or self.provider,
                                predictor or self.predictor, self.identity, monotonic=lambda: self.elapsed, sleep=lambda _: None)

    def run_work(self, work):
        return self.pipeline.process(self.message(self.jobs.get_work(work.work_id)))

    def downloaded(self, work):
        work = self.jobs.get_work(work.work_id)
        return AnalysisResult.model_validate(self.objects.read(work, work.result))

    def event(self, work):
        return {"Records": [{"eventSource": "aws:sqs", "messageId": "fixture-message",
                            "body": self.message(work).model_dump_json()}]}

    def test_pages_duplicates_skips_counts_and_utc(self):
        self.data["pages"]["fixture-page-2"]["comments"].extend([
            {"id": "unknown", "author": None, "published_at": "2026-10-12T14:20:00+05:30", "likes": 0, "text": "unknown"},
            {"id": "large", "author": None, "published_at": "2026-10-12T08:00:00Z", "likes": 0, "text": "x" * 5001}])
        self.pipeline = self.make_pipeline(FixtureCommentProvider(self.config, self.data))
        work = self.admit().work
        self.assertTrue(self.run_work(work))
        result = self.downloaded(work)
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.progress.fetched, current.progress.duplicates_removed), (7, 1))
        self.assertEqual((result.summary.received, result.summary.analyzed, result.summary.skipped), (6, 3, 3))
        self.assertEqual(result.summary.skipped_by_reason.model_dump(), {"empty_text": 1, "no_known_features": 1, "text_too_long": 1})
        self.assertEqual(result.summary.coverage, .5)
        self.assertEqual([r.id for r in result.comments], ["fixture-1", "fixture-2", "fixture-3", "fixture-4", "unknown", "large"])
        self.assertIsNone(result.comments[-1].text)
        self.assertFalse(any(len(t or "") > 5000 for batch in self.predictor.calls for t in batch))
        self.assertTrue(result.comments[4].published_at.isoformat().endswith("+00:00"))
        self.assertFalse(result.sampling.has_more)
        self.assertEqual(current.quota_remaining, 0)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)

    def test_empty_video_succeeds_without_inference(self):
        self.data["pages"] = {"": {"comments": [], "next_page_token": None}}
        self.pipeline = self.make_pipeline(FixtureCommentProvider(self.config, self.data))
        work = self.admit().work
        self.assertTrue(self.run_work(work))
        result = self.downloaded(work)
        self.assertEqual((result.state, result.summary.received, result.summary.coverage), ("SUCCEEDED", 0, 0))
        self.assertIsNone(result.sampling.sample_start)
        self.assertEqual(self.predictor.calls, [])

    def test_raw_sample_limit_does_not_refill_duplicates(self):
        self.data["pages"][""]["comments"][1] = copy.deepcopy(self.data["pages"][""]["comments"][0])
        self.pipeline = self.make_pipeline(FixtureCommentProvider(self.config, self.data))
        work = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=3)).work
        with patch.object(self.pipeline.provider, "comments", wraps=self.pipeline.provider.comments) as calls:
            self.assertTrue(self.run_work(work))
        result = self.downloaded(work)
        self.assertEqual((result.state, result.summary.received, result.duplicates_removed), ("SUCCEEDED", 2, 1))
        self.assertTrue(result.sampling.has_more)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(calls.call_args.args[-1], 3)

    def test_fetch_retries_count_quota_and_produce_partial_sample(self):
        work = self.admit().work
        original = self.provider.comments
        def page(video, token, size):
            if token:
                raise ProviderError("UPSTREAM_UNAVAILABLE", retryable=True)
            return original(video, token, size)
        with patch.object(self.provider, "comments", side_effect=page) as calls:
            self.assertTrue(self.run_work(work))
        result = self.downloaded(work)
        self.assertEqual((result.state, result.partial_reason, result.summary.received), ("PARTIAL", "FETCH_RETRIES_EXHAUSTED", 3))
        self.assertEqual(calls.call_count, 4)
        quota = self.store.get("QUOTA#" + work.quota_day).data
        self.assertEqual((quota["spent"], quota["reserved"]), (5, 0))

    def test_permanent_provider_error_has_no_retry_or_partial_result(self):
        work = self.admit().work
        with patch.object(self.provider, "comments", side_effect=ProviderError("COMMENTS_DISABLED")) as calls:
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code, calls.call_count), ("FAILED", "COMMENTS_DISABLED", 1))
        self.assertIsNone(current.result)

    def test_no_sample_retry_exhaustion_fails_and_releases_allowance(self):
        work = self.admit().work
        with patch.object(self.provider, "video", side_effect=ProviderError("UNAVAILABLE", retryable=True)) as calls:
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code, calls.call_count), ("FAILED", "FETCH_RETRIES_EXHAUSTED", 3))
        self.assertEqual(self.store.get("QUOTA#" + work.quota_day).data["reserved"], 0)

    def test_page_budget_and_token_cycle_are_bounded(self):
        self.data["pages"][""]["comments"] = []
        self.data["pages"]["fixture-page-2"]["comments"] = []
        self.data["pages"]["fixture-page-2"]["next_page_token"] = "more"
        self.pipeline = self.make_pipeline(FixtureCommentProvider(self.config, self.data))
        work = self.admit().work  # Maximum pages is ten; a missing fixture token fails.
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "INVALID_PROVIDER_RESPONSE")
        self.data["pages"][""]["next_page_token"] = "same"
        self.data["pages"][""]["comments"] = [{"id": "small", "author": None, "published_at": "2026-10-12T09:00:00Z", "likes": 0, "text": "good"}]
        self.data["pages"]["same"] = copy.deepcopy(self.data["pages"]["fixture-page-2"])
        self.data["pages"]["same"]["next_page_token"] = "same"
        self.pipeline = self.make_pipeline(FixtureCommentProvider(self.config, self.data))
        other = self.admit(key=None, input=AnalysisInput(video_url=self.input.video_url, max_comments=4)).work
        self.assertTrue(self.run_work(other))
        # max_comments=4 permits one page: returns a bounded partial, no refill.
        self.assertEqual(self.downloaded(other).partial_reason, "PAGE_LIMIT_REACHED")
        cyclic = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=999)).work
        with patch.object(self.pipeline.provider, "comments", wraps=self.pipeline.provider.comments) as pages:
            self.assertTrue(self.run_work(cyclic))
        self.assertEqual(self.jobs.get_work(cyclic.work_id).failure.code, "INVALID_PROVIDER_RESPONSE")
        self.assertEqual(pages.call_count, 2)

    def test_soft_deadline_resume_skips_completed_page(self):
        work = self.admit().work
        original = self.provider.comments
        def page(*args):
            result = original(*args)
            self.elapsed = 149
            return result
        original_save = self.pipeline._save
        def save(leased, checkpoint, *args):
            saved = original_save(leased, checkpoint, *args)
            if checkpoint.fetched:
                self.elapsed = 151
            return saved
        with patch.object(self.provider, "comments", side_effect=page), patch.object(self.pipeline, "_save", side_effect=save):
            self.assertTrue(self.run_work(work))
        queued = self.jobs.get_work(work.work_id)
        self.assertEqual((queued.state, queued.dispatch_generation), ("QUEUED", 1))
        self.assertEqual(queued.deadline_at, work.deadline_at)
        self.assertIsNotNone(self.store.get(f"OUTBOX#{work.work_id}#1"))
        self.elapsed = 0
        with patch.object(self.provider, "video", wraps=self.provider.video) as metadata, patch.object(self.provider, "comments", wraps=original) as pages:
            self.assertTrue(self.run_work(queued))
        self.assertEqual(pages.call_args.args[1], "fixture-page-2")
        self.assertEqual(pages.call_count, 1)
        # Fixture comments() itself checks video identity; no metadata fetch was resumed.
        self.assertEqual(metadata.call_count, 1)
        self.assertEqual(self.downloaded(work).summary.received, 4)

    def test_crash_after_inference_checkpoint_resumes_remaining_indices(self):
        work = self.admit().work
        save = self.pipeline._save
        def crash(leased, checkpoint, *args):
            saved = save(leased, checkpoint, *args)
            if len(checkpoint.predictions) == 2:
                raise SystemExit("simulated hard crash")
            return saved
        with patch.object(self.pipeline, "_save", side_effect=crash), self.assertRaises(SystemExit):
            self.run_work(work)
        self.now += timedelta(seconds=self.config.worker.lease_seconds + 1)
        self.assertTrue(self.jobs.recover(work.work_id))
        self.predictor.calls.clear()
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.predictor.calls, [["Terrible video", "ordinary video"]])
        self.assertEqual([r.index for r in self.downloaded(work).comments], [0, 1, 2, 3])

    def test_upload_crashes_recover_without_restarting_fetch(self):
        work = self.admit().work
        upload = self.objects.write_result
        def crash(*args):
            upload(*args)
            raise OSError("synthetic secret that must not be logged")
        with patch.object(self.objects, "write_result", side_effect=crash), self.assertLogs("src.Sentiment_analysis.pipeline.analysis", level="WARNING") as logs:
            self.assertTrue(self.run_work(work))
        self.assertNotIn("synthetic secret", " ".join(logs.output))
        self.assertEqual(self.jobs.get_work(work.work_id).state, "QUEUED")
        self.predictor.calls.clear()
        with patch.object(self.provider, "comments", side_effect=AssertionError("must not refetch")):
            self.assertTrue(self.run_work(work))
        self.assertEqual(self.predictor.calls, [])
        self.assertEqual(self.jobs.get_work(work.work_id).lease_token, 2)
        self.assertEqual(self.downloaded(work).summary.received, 4)

    def test_checkpoint_upload_before_pointer_crash_refetches_bounded_call(self):
        work = self.admit().work
        upload = self.objects.write_checkpoint
        once = [False]
        def crash(leased, token, payload):
            pointer = upload(leased, token, payload)
            if payload["fetched"] and not once[0]:
                once[0] = True
                raise OSError("response loss")
            return pointer
        with patch.object(self.objects, "write_checkpoint", side_effect=crash):
            self.assertTrue(self.run_work(work))
        queued = self.jobs.get_work(work.work_id)
        checkpoint = WorkerCheckpoint.model_validate(self.objects.read_checkpoint(queued))
        self.assertEqual((checkpoint.fetched, checkpoint.call_attempts), (0, 1))
        self.assertTrue(self.run_work(queued))
        self.assertEqual(self.downloaded(work).summary.received, 4)
        self.assertEqual(self.store.get("QUOTA#" + work.quota_day).data["spent"], 4)

    def test_result_too_large_fails_explicitly(self):
        work = self.admit().work
        with patch.object(self.objects, "write_result", side_effect=JobError("RESULT_TOO_LARGE")):
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code), ("FAILED", "RESULT_TOO_LARGE"))
        self.assertEqual(current.quota_remaining, 0)

    def test_checkpoint_byte_limit_never_silently_truncates(self):
        self.objects.settings = self.config.model_copy(update={"worker": self.config.worker.model_copy(update={"maximum_result_bytes": 100})})
        work = self.admit().work
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "RESULT_TOO_LARGE")
        self.assertEqual(self.predictor.calls, [])

    def test_deadline_and_stale_lease_cannot_publish(self):
        work = self.admit().work
        def advance(*args):
            self.now = work.deadline_at + timedelta(seconds=1)
            return self.provider.metadata
        with patch.object(self.provider, "video", side_effect=advance):
            self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "JOB_DEADLINE_EXCEEDED")
        self.assertEqual(self.predictor.calls, [])

    def test_release_and_recovery_attempt_limits(self):
        work = self.admit().work
        wrong = self.message(work).model_copy(update={"release_id": "sr_" + "9" * 64})
        self.assertFalse(self.pipeline.process(wrong))
        self.assertEqual(self.jobs.get_work(work.work_id).lease_token, 0)
        leased = self.claim(work)
        for _ in range(self.config.worker.maximum_attempts):
            self.jobs.recover(work.work_id, token=leased.lease_token)
            if _ < self.config.worker.maximum_attempts - 1:
                leased = self.claim(self.jobs.get_work(work.work_id))
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "WORKER_ATTEMPTS_EXHAUSTED")
        self.assertEqual(self.predictor.calls, [])

    def test_duplicate_active_delivery_does_not_steal_lease(self):
        work = self.admit().work
        leased = self.claim(work)
        self.assertEqual(self.pipeline.handle_sqs(self.event(work)), {"batchItemFailures": []})
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.lease_token, current.state), (leased.lease_token, "FETCHING"))

    def test_stale_worker_cannot_recover_newer_lease(self):
        work = self.admit().work
        newer = []
        def replace_lease(*args):
            self.now += timedelta(seconds=self.config.worker.lease_seconds + 1)
            self.jobs.recover(work.work_id)
            newer.append(self.claim(self.jobs.get_work(work.work_id)))
            raise OSError("old worker dependency failed")
        with patch.object(self.provider, "video", side_effect=replace_lease):
            self.assertFalse(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.lease_token, current.dispatch_generation), ("FETCHING", newer[0].lease_token, 1))

    def test_call_attempt_counter_survives_hard_crashes(self):
        work = self.admit().work
        with patch.object(self.provider, "video", side_effect=SystemExit("crash after call")) as calls:
            for _ in range(3):
                with self.assertRaises(SystemExit):
                    self.run_work(work)
                self.now += timedelta(seconds=self.config.worker.lease_seconds + 1)
                self.jobs.recover(work.work_id)
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual(calls.call_count, 3)
        self.assertEqual((current.state, current.failure.code), ("FAILED", "FETCH_RETRIES_EXHAUSTED"))

    def test_rate_limit_yields_before_provider_and_failed_recovery_retries_sqs(self):
        work = self.admit().work
        with patch.object(self.jobs, "spend_attempt", side_effect=JobError("RATE_LIMITED")), patch.object(self.provider, "video") as metadata:
            self.assertTrue(self.run_work(work))
        metadata.assert_not_called()
        self.assertEqual(self.jobs.get_work(work.work_id).dispatch_generation, 1)
        with patch.object(self.jobs, "spend_attempt", side_effect=JobError("RATE_LIMITED")), \
                patch.object(self.jobs, "recover", side_effect=OSError("database down")):
            self.assertEqual(self.pipeline.handle_sqs(self.event(self.jobs.get_work(work.work_id))),
                             {"batchItemFailures": [{"itemIdentifier": "fixture-message"}]})

    def test_job_model_mismatch_fails_before_provider_or_prediction(self):
        work = self.admit().work
        item = self.store.get("WORK#" + str(work.work_id))
        data = copy.deepcopy(item.data)
        data["model"]["sha256"] = "9" * 64
        from src.Sentiment_analysis.repositories.dynamo import Change
        self.store.commit([Change(item.key, item.version, data)])
        with patch.object(self.provider, "video") as metadata:
            self.assertTrue(self.run_work(work))
        metadata.assert_not_called()
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "RELEASE_MISMATCH")
        self.assertEqual(self.predictor.calls, [])

    def test_remaining_lambda_time_yields_and_expired_queued_work_fails(self):
        work = self.admit().work
        context = type("Context", (), {"get_remaining_time_in_millis": lambda _: 20000})()
        self.assertTrue(self.pipeline.process(self.message(work), context=context))
        queued = self.jobs.get_work(work.work_id)
        self.assertEqual((queued.state, queued.dispatch_generation), ("QUEUED", 1))
        self.now = work.deadline_at
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "JOB_DEADLINE_EXCEEDED")

    def test_quota_denial_after_sample_is_partial_without_another_provider_call(self):
        work = self.admit().work
        spend = self.jobs.spend_attempt
        count = [0]
        def bounded(*args):
            count[0] += 1
            if count[0] > 2:
                raise JobError("YOUTUBE_QUOTA_EXHAUSTED")
            return spend(*args)
        with patch.object(self.jobs, "spend_attempt", side_effect=bounded), patch.object(self.provider, "comments", wraps=self.provider.comments) as pages:
            self.assertTrue(self.run_work(work))
        result = self.downloaded(work)
        self.assertEqual((result.state, result.partial_reason, result.summary.received), ("PARTIAL", "YOUTUBE_QUOTA_EXHAUSTED", 3))
        self.assertEqual(pages.call_count, 1)

    def test_prediction_reordering_is_joined_by_index_and_corruption_is_rejected(self):
        work = self.admit().work
        predict = self.predictor.predict
        def reversed_rows(texts):
            response = predict(texts)
            response["comments"].reverse()
            return response
        with patch.object(self.predictor, "predict", side_effect=reversed_rows):
            self.assertTrue(self.run_work(work))
        result = self.downloaded(work)
        self.assertEqual([r.text for r in result.comments], ["Wonderful explanation!", "  ", "Terrible video", "ordinary video"])

    def test_invalid_checkpoint_and_bad_prediction_fail_safely(self):
        work = self.admit().work
        leased = self.claim(work)
        pointer = self.objects.write_checkpoint(leased, leased.lease_token, {"schema_version": 99})
        self.jobs.heartbeat(work.work_id, leased.lease_token, checkpoint=pointer)
        self.jobs.recover(work.work_id, token=leased.lease_token)
        self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "INVALID_CHECKPOINT")
        other = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=999)).work
        with patch.object(self.predictor, "predict", return_value={"comments": []}):
            self.assertTrue(self.run_work(other))
        self.assertEqual(self.jobs.get_work(other.work_id).failure.code, "INVALID_PREDICTION")

    def test_runtime_fixture_gate_identity_and_imports(self):
        production = self.config.model_copy(update={"environment": "production"})
        with self.assertRaises(ValueError):
            FixtureCommentProvider(production, self.data)
        with self.assertRaises(ValueError):
            AnalysisPipeline(production, self.jobs, self.objects, self.provider, self.predictor, self.identity)
        with self.assertRaises(ValueError):
            WorkerConfigurationManager(self.config, image_code_revision="5" * 40).get_worker_config()
        code = "import sys; import src.Sentiment_analysis.workers.analyze; assert not any(k.split('.')[0] in {'numpy','sklearn','mlflow','dvc','pandas'} for k in sys.modules)"
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True)

    def test_worker_configuration_matches_packaged_metadata_without_loading_ml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_file, report_file = root / "model.joblib", root / "training.json"
            model_file.write_bytes(b"synthetic bundle; weights are checked by PredictionService")
            report = {"passed": True, "model_type": "TF-IDF + LogisticRegression", "model_sha256": self.model.sha256,
                      "preprocessing_sha256": self.model.preprocessing_sha256,
                      "label_mapping": {"0": "Negative", "1": "Neutral", "2": "Positive"}}
            report_file.write_text(json.dumps(report), encoding="utf-8")
            settings = self.config.model_copy(update={"model": self.config.model.model_copy(update={
                "model_file": model_file, "training_report": report_file})})
            config = WorkerConfigurationManager(settings, image_code_revision=CODE).get_worker_config()
            self.assertFalse(config.prediction.restore_with_dvc)
            self.assertEqual(config.prediction.text_column, "CommentText")
            self.assertEqual(config.identity, self.identity)
            for changes in [{"passed": False}, {"preprocessing_sha256": "9" * 64}, {"label_mapping": {"0": "Wrong"}}]:
                report_file.write_text(json.dumps({**report, **changes}), encoding="utf-8")
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    WorkerConfigurationManager(settings, image_code_revision=CODE).get_worker_config()

    def test_actual_final_result_byte_ceiling(self):
        from src.Sentiment_analysis.entity.jobs import ObjectPointer
        work = self.admit().work
        self.objects.settings = self.config.model_copy(update={"worker": self.config.worker.model_copy(update={"maximum_result_bytes": 500})})
        # Keep checkpoints available so this specifically exercises final JSON
        # serialization, rather than just the earlier checkpoint size failure.
        def checkpoint_pointer(leased, token, payload):
            return ObjectPointer(key=f"{self.config.storage.serving_prefix}/work/{work.work_id}/attempt/{token}/checkpoints/{'1' * 64}.json",
                                 sha256="1" * 64, bytes=1, expires_at=work.expires_at)
        with patch.object(self.objects, "write_checkpoint", side_effect=checkpoint_pointer):
            self.assertTrue(self.run_work(work))
        current = self.jobs.get_work(work.work_id)
        self.assertEqual((current.state, current.failure.code), ("FAILED", "RESULT_TOO_LARGE"))
        self.assertIsNone(current.result)

    def test_invalid_review_flag_fails_and_terminal_response_loss_is_idempotent(self):
        work = self.admit().work
        predict = self.predictor.predict
        def bad_flags(texts):
            response = predict(texts)
            for row in response["comments"]:
                if row["status"] == "analyzed":
                    row["needs_review"] = True
            return response
        with patch.object(self.predictor, "predict", side_effect=bad_flags):
            self.assertTrue(self.run_work(work))
        self.assertEqual(self.jobs.get_work(work.work_id).failure.code, "INVALID_PREDICTION")
        other = self.admit(input=AnalysisInput(video_url=self.input.video_url, max_comments=999)).work
        finish = self.jobs.finish
        def response_lost(*args, **kwargs):
            finish(*args, **kwargs)
            raise OSError("response lost after terminal commit")
        with patch.object(self.jobs, "finish", side_effect=response_lost):
            self.assertTrue(self.run_work(other))
        self.assertTrue(self.pipeline.process(self.message(other)))
        self.assertEqual(self.jobs.get_work(other.work_id).state, "SUCCEEDED")
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)

    def test_lambda_record_failures_and_warm_runtime_reuse(self):
        work = self.admit().work
        with patch("src.Sentiment_analysis.workers.analyze.worker_runtime", return_value=self.pipeline):
            self.assertEqual(handler(self.event(work), None), {"batchItemFailures": []})
        with patch("src.Sentiment_analysis.workers.analyze.worker_runtime", side_effect=ValueError("private secret")), self.assertLogs("src.Sentiment_analysis.workers.analyze", level="WARNING") as logs:
            self.assertEqual(handler(self.event(work), None), {"batchItemFailures": [{"itemIdentifier": "fixture-message"}]})
        self.assertNotIn("private secret", " ".join(logs.output))
        bad = self.event(work)
        bad["Records"][0]["body"] = "invalid"
        self.assertEqual(self.pipeline.handle_sqs(bad), {"batchItemFailures": [{"itemIdentifier": "fixture-message"}]})
        settings = self.config.model_copy(update={"storage": self.config.storage.model_copy(update={"table_name": "serving-test"})})
        worker_runtime.cache_clear()
        self.addCleanup(worker_runtime.cache_clear)
        with patch("src.Sentiment_analysis.config.worker_runtime.ServingConfigurationManager") as manager, \
                patch("src.Sentiment_analysis.config.worker_runtime.build_worker_pipeline", return_value=self.pipeline) as build, \
                patch.dict("os.environ", {"SERVING_FIXTURE_PATH": str(ROOT / "examples/worker-fixture.json"),
                                          "SERVING_IMAGE_CODE_REVISION": CODE, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}):
            manager.return_value.get_serving_config.return_value = settings
            self.assertIs(worker_runtime(), worker_runtime())
            self.assertEqual(build.call_count, 1)


if __name__ == "__main__":
    unittest.main()
