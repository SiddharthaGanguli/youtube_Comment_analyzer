"""Claim -> resume -> fetch -> infer -> publish. Recovery uses the durable outbox."""

import logging
import math
import random
import time
from datetime import timedelta

from src.Sentiment_analysis.components.analysis import AnalysisProcessor, update_checkpoint
from src.Sentiment_analysis.entity.analysis import Failure, TERMINAL_STATES, Video, WorkMessage
from src.Sentiment_analysis.entity.worker import WorkerCheckpoint, WorkerIdentity
from src.Sentiment_analysis.integrations.provider import ProviderError
from src.Sentiment_analysis.repositories.jobs import JobError
from src.Sentiment_analysis.services.telemetry import Telemetry

logger = logging.getLogger(__name__)
FAILURES = {
    "RELEASE_MISMATCH": "Worker release differs from the accepted analysis",
    "WORKER_ATTEMPTS_EXHAUSTED": "Worker recovery attempts exhausted",
    "VIDEO_NOT_FOUND": "Public video was not found",
    "VIDEO_NOT_PUBLIC": "Video is not publicly available",
    "COMMENTS_DISABLED": "Comments are disabled for this video",
    "INVALID_PROVIDER_RESPONSE": "Comment provider returned an invalid response",
    "PAGE_LIMIT_REACHED": "Comment page limit reached without a usable sample",
    "FETCH_RETRIES_EXHAUSTED": "Comment fetch attempts exhausted",
    "YOUTUBE_QUOTA_EXHAUSTED": "Comment provider allowance is exhausted",
    "PROVIDER_CREDENTIALS_INVALID": "Comment provider credentials need operator attention",
    "PROVIDER_ACCESS_DENIED": "Comment provider access was denied",
    "PROVIDER_UNAVAILABLE": "Comment provider is temporarily unavailable",
    "RESULT_TOO_LARGE": "Analysis exceeds the configured result size limit",
    "INVALID_CHECKPOINT": "Saved analysis progress is invalid",
    "INVALID_PREDICTION": "Model output could not be validated",
}


class AttemptYield(Exception):
    def __init__(self, *, not_before=None):
        self.not_before = not_before
        super().__init__("Worker invocation yielded")


class AnalysisPipeline:
    def __init__(self, settings, jobs, objects, provider, predictor, identity, *, monotonic=None, sleep=None):
        self.settings, self.jobs, self.objects, self.provider = settings, jobs, objects, provider
        self.identity = WorkerIdentity.model_validate(identity.model_dump())
        if self.identity.model != self._assigned_model() or self.identity.code_revision != settings.model.code_revision:
            raise ValueError("Worker identity differs from the assigned settings")
        if getattr(provider, "is_fixture", False) and settings.environment != "development":
            raise ValueError("Fixture providers are allowed only in development")
        if not getattr(provider, "is_fixture", False):
            from src.Sentiment_analysis.entity.serving import YouTubeSettings
            options = YouTubeSettings.model_validate(settings.youtube.model_dump())
            if not options.live_enabled:
                raise ValueError("Live comment fetching is disabled")
        self.processor = AnalysisProcessor(settings, predictor)
        self.monotonic, self.sleep = monotonic or time.monotonic, sleep or time.sleep
        self.telemetry = Telemetry(settings.environment, "worker")

    def _assigned_model(self):
        from src.Sentiment_analysis.entity.analysis import ModelReference
        model = self.settings.model
        return ModelReference(release_id=model.release_id, sha256=model.sha256,
                              preprocessing_sha256=model.preprocessing_sha256,
                              review_confidence_threshold=self.settings.worker.review_confidence_threshold,
                              result_schema_version=model.result_schema_version)

    def _guard(self, work, started, context):
        now = self.jobs.clock()
        if now >= work.deadline_at:
            self.jobs.expire(work.work_id)
            raise JobError("LEASE_LOST")
        if work.lease_until is None or now >= work.lease_until:
            raise JobError("LEASE_LOST")
        reserve = self.settings.worker.timeout_seconds - self.settings.worker.soft_deadline_seconds
        if (self.monotonic() - started >= self.settings.worker.soft_deadline_seconds or
                context is not None and context.get_remaining_time_in_millis() <= reserve * 1000):
            raise AttemptYield()

    def _save(self, work, checkpoint, started, context):
        self._guard(work, started, context)
        work = self.jobs.heartbeat(work.work_id, work.lease_token)
        pointer = self.objects.write_checkpoint(work, work.lease_token, checkpoint.model_dump(mode="json"))
        self._guard(work, started, context)
        return self.jobs.heartbeat(work.work_id, work.lease_token, progress=checkpoint.progress, checkpoint=pointer,
                                   sampling=self.processor.sampling(checkpoint, work.input.max_comments))

    def _restore(self, work):
        raw = self.objects.read_checkpoint(work)
        checkpoint = WorkerCheckpoint.model_validate(raw) if raw is not None else WorkerCheckpoint()
        if (checkpoint.progress != work.progress or checkpoint.fetched > work.input.max_comments or
                checkpoint.video and checkpoint.video.id != work.input.video_id or
                checkpoint.visibility_lease_token > work.lease_token or
                checkpoint.retry_not_before and checkpoint.retry_not_before < work.created_at or
                checkpoint.fetched_at and not work.created_at <= checkpoint.fetched_at <= self.jobs.clock() or
                any(r.text is not None and len(r.text) > self.settings.worker.maximum_comment_characters
                    for r in checkpoint.pending)):
            raise ValueError("Checkpoint differs from accepted work")
        if checkpoint.predictions:
            # Validate resumed predictions, including review flags and all public invariants.
            partial = update_checkpoint(checkpoint, pending=(), fetched=len(checkpoint.predictions) + checkpoint.duplicates_removed)
            self.processor.result(partial, work)
        return checkpoint

    def _provider_fault(self, error):
        if error.circuit_reason:
            self.jobs.circuit.open(error.circuit_reason)

    def _wait_retry(self, work, checkpoint, started, context):
        """A saved upstream cooldown also applies after another worker claims the job."""
        while checkpoint.retry_not_before and checkpoint.retry_not_before > self.jobs.clock():
            try:
                self._guard(work, started, context)
            except AttemptYield:
                raise AttemptYield(not_before=checkpoint.retry_not_before) from None
            delay = (checkpoint.retry_not_before - self.jobs.clock()).total_seconds()
            reserve = self.settings.worker.timeout_seconds - self.settings.worker.soft_deadline_seconds
            headroom = min(self.settings.worker.soft_deadline_seconds - (self.monotonic() - started),
                           (work.deadline_at - self.jobs.clock()).total_seconds())
            if context is not None:
                headroom = min(headroom, context.get_remaining_time_in_millis() / 1000 - reserve)
            if delay + self.settings.youtube.http_deadline_seconds >= headroom:
                raise AttemptYield(not_before=checkpoint.retry_not_before)
            # Renew ownership while waiting; a long cooldown never silently runs out its lease.
            work = self.jobs.heartbeat(work.work_id, work.lease_token)
            self.sleep(min(delay, self.settings.worker.heartbeat_seconds))
        return work

    def _schedule_retry(self, work, checkpoint, error, started, context):
        if error.retry_after_seconds is None:
            self._guard(work, started, context)
            self.sleep(random.uniform(.1, min(2.0, 2 ** max(checkpoint.call_attempts, checkpoint.visibility_attempts))))
            return work, checkpoint
        delay = error.retry_after_seconds
        checkpoint = update_checkpoint(checkpoint, retry_not_before=self.jobs.clock() + timedelta(seconds=delay))
        try:
            work = self._save(work, checkpoint, started, context)
        except AttemptYield:
            # The provider can return just after the soft deadline. Retain the
            # known cooldown in the recovery transaction even if S3 cannot save it.
            raise AttemptYield(not_before=checkpoint.retry_not_before) from None
        except JobError as error:
            if error.code in {"LEASE_LOST", "RESULT_TOO_LARGE"}:
                raise
            raise AttemptYield(not_before=checkpoint.retry_not_before) from None
        except Exception:
            logger.warning("Analysis worker operation failed")
            self.telemetry.emit("worker_dependency_error", {"WorkerDependencyFailureCount": 1},
                                outcome="retry", work_id=work.work_id, attempt=work.lease_token)
            raise AttemptYield(not_before=checkpoint.retry_not_before) from None
        return work, checkpoint

    def _recheck_visibility(self, work, checkpoint, started, context):
        # Fixture metadata is immutable. Saved live data needs a new public check
        # on every new lease, even when fetching/inference previously completed.
        if (getattr(self.provider, "is_fixture", False) or checkpoint.video is None or
                checkpoint.visibility_lease_token == work.lease_token):
            return work, checkpoint
        maximum = self.settings.youtube.maximum_http_attempts_per_call
        while True:
            if checkpoint.visibility_attempts >= maximum:
                raise ProviderError("FETCH_RETRIES_EXHAUSTED", retryable=True)
            work = self._wait_retry(work, checkpoint, started, context)
            self._guard(work, started, context)
            work = self.jobs.spend_attempt(work.work_id, work.lease_token)
            checkpoint = update_checkpoint(checkpoint, visibility_attempts=checkpoint.visibility_attempts + 1)
            work = self._save(work, checkpoint, started, context)
            try:
                self._guard(work, started, context)
                video = self.provider.video(work.input.video_id)
                if not isinstance(video, Video) or video.id != work.input.video_id:
                    raise ProviderError("INVALID_PROVIDER_RESPONSE")
                checkpoint = update_checkpoint(checkpoint, video=video, visibility_attempts=0,
                                               visibility_lease_token=work.lease_token, retry_not_before=None)
                work = self._save(work, checkpoint, started, context)
                return work, checkpoint
            except ProviderError as error:
                self._provider_fault(error)
                if not error.retryable or checkpoint.visibility_attempts >= maximum:
                    raise ProviderError("FETCH_RETRIES_EXHAUSTED" if error.retryable else error.code,
                                        retryable=error.retryable) from None
                work, checkpoint = self._schedule_retry(work, checkpoint, error, started, context)

    def _fetch(self, work, checkpoint, started, context):
        maximum = self.settings.youtube.maximum_http_attempts_per_call
        while not checkpoint.fetch_complete:
            try:
                self._guard(work, started, context)
            except AttemptYield:
                raise AttemptYield(not_before=checkpoint.retry_not_before) from None
            metadata = checkpoint.video is None
            if not metadata and (checkpoint.fetched >= work.input.max_comments or
                    len(checkpoint.page_tokens) >= math.ceil(work.input.max_comments / self.settings.youtube.page_size) or
                    checkpoint.page_tokens and checkpoint.next_page_token is None):
                checkpoint = self.processor.finish_fetch(checkpoint, work.input.max_comments)
                work = self._save(work, checkpoint, started, context)
                break
            try:
                if checkpoint.call_attempts >= maximum:
                    raise ProviderError("FETCH_RETRIES_EXHAUSTED", retryable=True)
                work = self._wait_retry(work, checkpoint, started, context)
                # Spend first, persist the bounded call counter next, then call the provider.
                # A crash before the provider can waste an allowance, never an uncounted HTTP attempt.
                work = self.jobs.spend_attempt(work.work_id, work.lease_token)
                checkpoint = update_checkpoint(checkpoint, call_attempts=checkpoint.call_attempts + 1)
                work = self._save(work, checkpoint, started, context)
                self._guard(work, started, context)
                if metadata:
                    video = self.provider.video(work.input.video_id)
                    if not isinstance(video, Video) or video.id != work.input.video_id:
                        raise ProviderError("INVALID_PROVIDER_RESPONSE")
                    checkpoint = update_checkpoint(checkpoint, video=video, fetched_at=self.jobs.clock(), call_attempts=0,
                                                   visibility_lease_token=work.lease_token, retry_not_before=None)
                else:
                    size = min(self.settings.youtube.page_size, work.input.max_comments - checkpoint.fetched)
                    page = self.provider.comments(work.input.video_id, checkpoint.next_page_token, size)
                    checkpoint = self.processor.accept_page(checkpoint, page, size)
                    checkpoint = update_checkpoint(checkpoint, retry_not_before=None)
                work = self._save(work, checkpoint, started, context)
            except ProviderError as error:
                self._provider_fault(error)
                if error.retryable and checkpoint.call_attempts < maximum:
                    work, checkpoint = self._schedule_retry(work, checkpoint, error, started, context)
                    continue
                if (error.retryable or error.code == "YOUTUBE_QUOTA_EXHAUSTED") and checkpoint.fetched - checkpoint.duplicates_removed:
                    checkpoint = self.processor.finish_fetch(checkpoint, work.input.max_comments,
                                                              partial_reason="FETCH_RETRIES_EXHAUSTED" if error.retryable else error.code)
                    work = self._save(work, checkpoint, started, context)
                    break
                raise ProviderError("FETCH_RETRIES_EXHAUSTED" if error.retryable else error.code,
                                    retryable=error.retryable) from None
            except JobError as error:
                if error.code == "YOUTUBE_QUOTA_EXHAUSTED" and checkpoint.fetched - checkpoint.duplicates_removed:
                    checkpoint = self.processor.finish_fetch(checkpoint, work.input.max_comments,
                                                              partial_reason="YOUTUBE_QUOTA_EXHAUSTED")
                    work = self._save(work, checkpoint, started, context)
                    break
                raise
        return work, checkpoint

    def _fail(self, work, code, *, retryable=False):
        if code not in FAILURES:
            code = "INVALID_PROVIDER_RESPONSE"
        self.jobs.finish(work.work_id, work.lease_token, state="FAILED",
                         failure=Failure(code=code, message=FAILURES[code], retryable=retryable))

    def _recover(self, work, *, not_before=None):
        try:
            recovered = self.jobs.recover(work.work_id, token=work.lease_token, not_before=not_before)
            current = self.jobs.get_work(work.work_id)
            return recovered or bool(current and current.state in TERMINAL_STATES)
        except Exception:
            return False

    def process(self, message, *, context=None):
        """True means handled/duplicate; False asks SQS to retry this record."""
        if message.release_id != self.identity.model.release_id:
            self.telemetry.emit("worker_delivery", {"WorkerDeliveryCount": 1, "MisroutedRecordCount": 1},
                                outcome="misrouted", work_id=message.work_id)
            return False  # Misrouted messages go to the DLQ, never another release's work.
        work = self.jobs.claim(message)
        if work is None:
            self.telemetry.emit("worker_delivery", {"WorkerDeliveryCount": 1, "DuplicateDeliveryCount": 1},
                                outcome="duplicate", work_id=message.work_id)
            self.jobs.expire(message.work_id)
            return True
        started = self.monotonic()
        self.telemetry.emit("worker_delivery", {"WorkerDeliveryCount": 1}, outcome="accepted", work_id=work.work_id,
                            attempt=work.lease_token, release_id=work.model.release_id)
        checkpoint = None
        try:
            if work.model != self.identity.model:
                self._fail(work, "RELEASE_MISMATCH")
                return True
            if work.lease_token > self.settings.worker.maximum_attempts:
                self._fail(work, "WORKER_ATTEMPTS_EXHAUSTED")
                return True
            try:
                checkpoint = self._restore(work)
            except (ValueError, TypeError, KeyError):
                self._fail(work, "INVALID_CHECKPOINT")
                return True
            work, checkpoint = self._recheck_visibility(work, checkpoint, started, context)
            work, checkpoint = self._fetch(work, checkpoint, started, context)
            if checkpoint.partial_reason and not checkpoint.fetched - checkpoint.duplicates_removed:
                self._fail(work, checkpoint.partial_reason)
                return True
            work = self.jobs.heartbeat(work.work_id, work.lease_token, state="INFERENCING")
            while checkpoint.pending:
                self._guard(work, started, context)
                work = self.jobs.heartbeat(work.work_id, work.lease_token)
                try:
                    checkpoint = self.processor.infer_batch(checkpoint)
                except (ValueError, TypeError, KeyError):
                    self._fail(work, "INVALID_PREDICTION")
                    return True
                work = self._save(work, checkpoint, started, context)
            try:
                result = self.processor.result(checkpoint, work)
            except (ValueError, TypeError, KeyError):
                self._fail(work, "INVALID_PREDICTION")
                return True
            self._guard(work, started, context)
            work = self.jobs.heartbeat(work.work_id, work.lease_token, state="SAVING")
            pointer = self.objects.write_result(work, work.lease_token, result)
            self._guard(work, started, context)
            self.jobs.finish(work.work_id, work.lease_token, state=result.state, result=pointer,
                             partial_reason=result.partial_reason)
            return True
        except ProviderError as error:
            self._fail(work, error.code, retryable=error.retryable)
            return True
        except JobError as error:
            if error.code == "RESULT_TOO_LARGE":
                self._fail(work, error.code)
                return True
            if error.code == "YOUTUBE_QUOTA_EXHAUSTED":
                self._fail(work, error.code, retryable=True)
                return True
            if error.code == "DEPENDENCY_UNAVAILABLE":
                self._fail(work, "PROVIDER_CREDENTIALS_INVALID")
                return True
            if error.code == "LEASE_LOST":
                self.jobs.expire(work.work_id)
                return True  # Current lease/reconciler owns any remaining recovery.
            if error.code in {"RATE_LIMITED", "SYSTEM_BUSY"}:
                return self._recover(work, not_before=checkpoint.retry_not_before if checkpoint else None)
            raise
        except AttemptYield as yielded:
            return self._recover(work, not_before=yielded.not_before)
        except Exception:
            logger.warning("Analysis worker operation failed")
            self.telemetry.emit("worker_dependency_error", {"WorkerDependencyFailureCount": 1},
                                outcome="retry", work_id=work.work_id, attempt=work.lease_token)
            # Only this invocation's token can request recovery. Never read and
            # recover a newer worker's token after losing ownership.
            return self._recover(work, not_before=checkpoint.retry_not_before if checkpoint else None)

    def handle_sqs(self, event, context=None):
        records = event.get("Records") if isinstance(event, dict) else None
        if not isinstance(records, list) or not records or any(
                not isinstance(r, dict) or not isinstance(r.get("messageId"), str) or not r["messageId"] for r in records):
            raise ValueError("Expected identifiable SQS records")
        failures = []
        for record in records:
            try:
                body = record.get("body")
                if record.get("eventSource") != "aws:sqs" or not isinstance(body, str) or len(body.encode()) > 2048:
                    raise ValueError("Invalid queue record")
                message = WorkMessage.model_validate_json(body)
                handled = self.process(message, context=context)
            except Exception:
                # No SDK error, message body, token, comment or signed URL enters logs.
                logger.warning("Analysis worker operation failed")
                handled = False
            if not handled:
                failures.append({"itemIdentifier": record["messageId"]})
        return {"batchItemFailures": failures}
