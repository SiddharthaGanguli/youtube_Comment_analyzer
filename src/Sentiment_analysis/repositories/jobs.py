"""Admission, ownership and worker coordination using conditional transactions."""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from src.Sentiment_analysis.entity.analysis import (
    Analysis, AnalysisInput, Failure, ModelReference, Progress, Sampling,
    TERMINAL_STATES, WorkMessage,
)
from src.Sentiment_analysis.entity.jobs import Admission, ObjectPointer, OwnedRequest, WorkRecord
from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.services.telemetry import Telemetry
from src.Sentiment_analysis.repositories.errors import JobError
from src.Sentiment_analysis.repositories.provider_circuit import ProviderCircuit

PACIFIC = ZoneInfo("America/Los_Angeles")
PURGE_DELAY_SECONDS = 3600


def epoch(value: datetime) -> int:
    return int(value.timestamp())


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quota_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).date().isoformat()


class JobRepository:
    def __init__(self, store, settings, *, clock=None, telemetry=None):
        self.store = store
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.telemetry = telemetry or Telemetry(settings.environment, "jobs")
        self.circuit = ProviderCircuit(store, clock=self.clock, telemetry=self.telemetry)

    @staticmethod
    def _version(item):
        return item.version if item else None

    def _change(self, key, item, data, **indexes):
        return Change(key, self._version(item), data, **indexes)

    def _work_change(self, item, work: WorkRecord):
        due = None if work.state in TERMINAL_STATES else min(
            work.deadline_at, work.lease_until or work.deadline_at)
        return self._change("WORK#" + str(work.work_id), item, work.model_dump(mode="json"),
                            due_family="work" if due else None, due_at=epoch(due) if due else None,
                            cleanup_at=epoch(work.expires_at) + PURGE_DELAY_SECONDS)

    def _request_change(self, item, request):
        return self._change("REQUEST#" + str(request.analysis_id), item, request.model_dump(mode="json"),
                            cleanup_at=epoch(request.expires_at) + PURGE_DELAY_SECONDS,
                            owner=digest(request.owner), created_at=epoch(request.created_at))

    def _outbox_change(self, work, now, *, not_before=None):
        message = WorkMessage(work_id=work.work_id, release_id=work.model.release_id,
                              dispatch_generation=work.dispatch_generation)
        key = f"OUTBOX#{work.work_id}#{work.dispatch_generation}"
        # Round future eligibility up so a fractional timestamp cannot release a
        # retry early. The pending outbox, rather than an SQS delay, holds waits
        # longer than one Lambda invocation or SQS's 15-minute delay maximum.
        eligible = math.ceil(not_before.timestamp()) if not_before is not None else None
        due = eligible if eligible is not None and eligible > epoch(now) else (
            epoch(now) + self.settings.queue.pending_outbox_retry_after_seconds)
        return Change(key, None, {"message": message.model_dump(mode="json"), "sent": False,
                                  "created_at": epoch(now), "sent_at": None, "not_before": eligible},
                      due_family="outbox", due_at=min(due, epoch(work.deadline_at)),
                      cleanup_at=epoch(work.expires_at) + PURGE_DELAY_SECONDS)

    def _quota_change(self, key, item, data):
        day = datetime.fromisoformat(key.removeprefix("QUOTA#")).replace(tzinfo=PACIFIC)
        cleanup = day + timedelta(days=8)
        return self._change(key, item, data, cleanup_at=epoch(cleanup))

    def get_work(self, work_id) -> WorkRecord | None:
        item = self.store.get("WORK#" + str(work_id))
        return WorkRecord.model_validate(item.data) if item else None

    def owned(self, owner: str, analysis_id, *, allow_deleted=False):
        item = self.store.get("REQUEST#" + str(analysis_id))
        if item is None or item.data["owner"] != owner:
            raise JobError("NOT_FOUND")
        request = OwnedRequest.model_validate(item.data)
        if request.expires_at <= self.clock() or (request.deleted and not allow_deleted):
            raise JobError("EXPIRED")
        work = self.get_work(request.work_id)
        if work is None or work.expires_at <= self.clock():
            raise JobError("EXPIRED")
        return request, work

    def status(self, owner: str, analysis_id) -> Analysis:
        request, work = self.owned(owner, analysis_id)
        return Analysis(analysis_id=request.analysis_id, video_id=work.input.video_id, state=work.state,
                        created_at=request.created_at, updated_at=max(request.created_at, work.updated_at),
                        deadline_at=work.deadline_at, expires_at=min(request.expires_at, work.expires_at),
                        cache_hit=request.cache_hit, model=work.model, sampling=work.sampling,
                        progress=work.progress, result_available=work.result is not None,
                        partial_reason=work.partial_reason, failure=work.failure)

    def _live_slots(self, item, now):
        slots = {}
        for request_id, slot in (item.data["slots"] if item else {}).items():
            if slot["expires_at"] <= epoch(now):
                continue
            work_item = self.store.get("WORK#" + slot["work_id"])
            if (work_item and work_item.data["state"] not in TERMINAL_STATES and
                    WorkRecord.model_validate(work_item.data).deadline_at <= now):
                self.expire(slot["work_id"])
                work_item = self.store.get("WORK#" + slot["work_id"])
            if work_item and work_item.data["state"] not in TERMINAL_STATES:
                # Do not free a global slot just because its lease is stale.
                slots[request_id] = slot
        return slots

    @staticmethod
    def _slot_cleanup(slots, now):
        # A short-lived cached request must not erase another job's active slot.
        return max([epoch(now), *(slot["expires_at"] for slot in slots.values())]) + PURGE_DELAY_SECONDS

    def admit(self, owner: str, key: UUID, input: AnalysisInput, model: ModelReference) -> Admission:
        if not isinstance(key, UUID) or not owner or len(owner) > 256:
            raise ValueError("Admission requires a verified owner and UUID idempotency key")
        if not isinstance(input, AnalysisInput) or input.max_comments > self.settings.youtube.maximum_comments:
            raise ValueError("Comment limit exceeds serving settings")
        if (model.sha256 != self.settings.model.sha256 or
                model.preprocessing_sha256 != self.settings.model.preprocessing_sha256 or
                model.review_confidence_threshold != self.settings.worker.review_confidence_threshold or
                (self.settings.model.release_id and model.release_id != self.settings.model.release_id)):
            raise ValueError("Admission release does not match serving settings")
        body_hash = digest(input.model_dump(mode="json"))
        owner_key = digest(owner)
        idem_key = f"IDEMP#{owner_key}#{key}"
        cache_key = digest({"input": input.model_dump(mode="json"), "scope": "public",
                            "order": "time", "replies": False, "release": model.release_id})
        for _ in range(8):
            now = self.clock()
            idem = self.store.get(idem_key)
            if idem and idem.data["expires_at"] > epoch(now):
                if idem.data["body_hash"] != body_hash:
                    raise JobError("IDEMPOTENCY_CONFLICT")
                request, work = self.owned(owner, idem.data["analysis_id"])
                self.telemetry.emit("admission", {"SubmissionCount": 1, "ReplayCount": 1}, outcome="replay", work_id=work.work_id)
                return Admission(request=request, work=work, replay=True)
            cache_item = self.store.get("CACHE#" + cache_key)
            work_item = self.store.get("WORK#" + cache_item.data["work_id"]) if cache_item else None
            work = WorkRecord.model_validate(work_item.data) if work_item else None
            if work and work.state not in TERMINAL_STATES and work.deadline_at <= now:
                self.expire(work.work_id)
                continue
            cache_hit = bool(work and work.result and work.fresh_until and work.fresh_until > now
                             and work.expires_at > now)
            join = bool(work and work.state not in TERMINAL_STATES and work.deadline_at > now)
            new_work = not cache_hit and not join
            slot_key = "LIMIT#SLOTS#" + owner_key
            slot_item = self.store.get(slot_key)
            slots = self._live_slots(slot_item, now)
            if not cache_hit and len(slots) >= self.settings.admission.user_maximum_active_references:
                raise JobError("ACTIVE_JOB_LIMIT")
            minute = epoch(now) // 60
            user_rate_key = f"LIMIT#POST#{owner_key}#{minute}"
            user_rate = self.store.get(user_rate_key)
            user_count = user_rate.data["count"] if user_rate else 0
            if user_count >= self.settings.admission.user_submissions_per_minute:
                raise JobError("RATE_LIMITED")
            changes = []
            if new_work:
                circuit = self.circuit.assert_available()
                changes.append(self.circuit.condition(circuit))
                global_item = self.store.get("LIMIT#GLOBAL#ACTIVE")
                active = global_item.data["count"] if global_item else 0
                global_rate_key = f"LIMIT#GLOBAL#POST#{minute}"
                global_rate = self.store.get(global_rate_key)
                count = global_rate.data["count"] if global_rate else 0
                if (active >= self.settings.admission.maximum_nonterminal_work or
                        count >= self.settings.admission.maximum_new_work_per_minute):
                    raise JobError("SYSTEM_BUSY")
                day = quota_day(now)
                quota_key = "QUOTA#" + day
                quota = self.store.get(quota_key)
                budget = self.settings.youtube.working_daily_quota_units
                q = dict(quota.data) if quota else {"spent": 0, "reserved": 0, "budget": budget}
                reserve = (1 + (input.max_comments + self.settings.youtube.page_size - 1) //
                           self.settings.youtube.page_size) * self.settings.youtube.maximum_http_attempts_per_call
                if q["spent"] + q["reserved"] + reserve > min(q["budget"], budget):
                    self.telemetry.emit("quota_denied", {"QuotaDeniedCount": 1}, error_code="YOUTUBE_QUOTA_EXHAUSTED")
                    raise JobError("YOUTUBE_QUOTA_EXHAUSTED")
                q["reserved"] += reserve
                work = WorkRecord(
                    work_id=uuid4(), input=input, model=model, cache_key=cache_key, state="QUEUED",
                    created_at=now, updated_at=now,
                    deadline_at=now + timedelta(seconds=self.settings.queue.job_deadline_seconds),
                    expires_at=now + timedelta(seconds=self.settings.cache.result_retention_seconds),
                    lease_until=None, lease_token=0, dispatch_generation=0, quota_day=day,
                    quota_remaining=reserve, terminal_released=False,
                    progress=Progress(fetched=0, duplicates_removed=0, analyzed=0, skipped=0),
                    sampling=Sampling(scope="public_top_level_comments", order="time", include_replies=False,
                                      requested_max_comments=input.max_comments, has_more=None,
                                      sample_start=None, sample_end=None),
                    checkpoint=None, result=None, fresh_until=None, partial_reason=None, failure=None)
                changes.extend([
                    self._work_change(None, work), self._outbox_change(work, now),
                    Change("OBJECTS#" + str(work.work_id), None,
                           {"work_id": str(work.work_id), "expires_at": epoch(work.expires_at)},
                           cleanup_at=epoch(work.expires_at)),
                    self._change("LIMIT#GLOBAL#ACTIVE", global_item, {"count": active + 1}),
                    self._change(global_rate_key, global_rate, {"count": count + 1}, cleanup_at=(minute + 1) * 60 + 3600),
                    self._quota_change(quota_key, quota, q),
                    self._change("CACHE#" + cache_key, cache_item, {"work_id": str(work.work_id)},
                                 cleanup_at=epoch(work.expires_at) + PURGE_DELAY_SECONDS),
                ])
            else:
                # Protect a join against a simultaneous completion or cache replacement.
                changes.append(Change(work_item.key, work_item.version, None, check_only=True))
            request = OwnedRequest(analysis_id=uuid4(), owner=owner, work_id=work.work_id,
                                   created_at=now, expires_at=min(work.expires_at, now + timedelta(
                                       seconds=self.settings.cache.request_retention_seconds)), cache_hit=cache_hit)
            if not cache_hit:
                slots[str(request.analysis_id)] = {"work_id": str(work.work_id), "expires_at": epoch(request.expires_at)}
            changes.extend([
                self._request_change(None, request),
                self._change(slot_key, slot_item, {"slots": slots}, cleanup_at=self._slot_cleanup(slots, now)),
                self._change(user_rate_key, user_rate, {"count": user_count + 1}, cleanup_at=(minute + 1) * 60 + 3600),
                self._change(idem_key, idem, {"body_hash": body_hash, "analysis_id": str(request.analysis_id),
                                            "expires_at": epoch(now) + self.settings.cache.idempotency_retention_seconds},
                             cleanup_at=epoch(now) + self.settings.cache.idempotency_retention_seconds + PURGE_DELAY_SECONDS),
            ])
            if self.store.commit(changes):
                self.telemetry.emit("admission", {"SubmissionCount": 1, "NewWorkCount": int(new_work),
                    "CacheHitCount": int(cache_hit), "JoinedWorkCount": int(join),
                    "QuotaReservedUnits": work.quota_remaining if new_work else 0},
                    outcome="accepted" if new_work else "cache_hit" if cache_hit else "joined", work_id=work.work_id,
                    release_id=work.model.release_id)
                return Admission(request=request, work=work, replay=False)
        raise JobError("SYSTEM_BUSY")

    def charge_read(self, owner: str):
        """The API calls this after authenticating, before a read route."""
        if not owner or len(owner) > 256:
            raise ValueError("Read admission requires a verified owner")
        for _ in range(8):
            minute = epoch(self.clock()) // 60
            key = f"LIMIT#GET#{digest(owner)}#{minute}"
            item = self.store.get(key)
            count = item.data["count"] if item else 0
            if count >= self.settings.admission.user_reads_per_minute:
                raise JobError("RATE_LIMITED")
            if self.store.commit([self._change(key, item, {"count": count + 1},
                                               cleanup_at=(minute + 1) * 60 + 3600)]):
                return
        raise JobError("SYSTEM_BUSY")

    def delete_owned(self, owner, analysis_id):
        for _ in range(8):
            request, _ = self.owned(owner, analysis_id, allow_deleted=True)
            if request.deleted:
                return
            item = self.store.get("REQUEST#" + str(request.analysis_id))
            if item is None:
                raise JobError("NOT_FOUND")
            request = OwnedRequest.model_validate(item.data)
            slots_key = "LIMIT#SLOTS#" + digest(owner)
            slot_item = self.store.get(slots_key)
            slots = dict(slot_item.data["slots"]) if slot_item else {}
            slots.pop(str(request.analysis_id), None)
            changes = [self._request_change(item, request.model_copy(update={"deleted": True}))]
            if slot_item:
                changes.append(self._change(slots_key, slot_item, {"slots": slots},
                                            cleanup_at=self._slot_cleanup(slots, self.clock())))
            if self.store.commit(changes):
                return
        raise JobError("SYSTEM_BUSY")

    def claim(self, message: WorkMessage) -> WorkRecord | None:
        for _ in range(8):
            now = self.clock()
            item = self.store.get("WORK#" + str(message.work_id))
            if item is None:
                return None
            work = WorkRecord.model_validate(item.data)
            if (work.state != "QUEUED" or work.deadline_at <= now or
                    message.release_id != work.model.release_id or
                    message.dispatch_generation != work.dispatch_generation):
                return None
            outbox_key = f"OUTBOX#{work.work_id}#{work.dispatch_generation}"
            outbox = self.store.get(outbox_key)
            if outbox and outbox.data.get("not_before") is not None and outbox.data["not_before"] > now.timestamp():
                # A prematurely delivered duplicate must not consume another
                # lease while the scheduled outbox remains pending.
                return None
            leased = work.model_copy(update={"state": "FETCHING", "updated_at": now,
                                            "lease_token": work.lease_token + 1,
                                            "lease_until": min(work.deadline_at, now + timedelta(
                                                seconds=self.settings.worker.lease_seconds))})
            if self.store.commit([self._work_change(item, leased),
                                  Change(outbox_key, self._version(outbox), None, check_only=True)]):
                return leased
        # A transaction conflict alone does not prove another worker claimed it.
        # Raise so the SQS handler retries rather than acknowledging a queued job.
        raise JobError("SYSTEM_BUSY")

    def _leased(self, work_id, token, now):
        item = self.store.get("WORK#" + str(work_id))
        work = WorkRecord.model_validate(item.data) if item else None
        if (work is None or work.state in TERMINAL_STATES or work.lease_token != token or
                work.lease_until is None or work.lease_until <= now or work.deadline_at <= now):
            raise JobError("LEASE_LOST")
        return item, work

    def heartbeat(self, work_id, token, *, state=None, progress=None, sampling=None, checkpoint=None):
        transitions = {"FETCHING": {"FETCHING", "INFERENCING"},
                       "INFERENCING": {"INFERENCING", "SAVING"}, "SAVING": {"SAVING"}}
        for _ in range(8):
            now = self.clock()
            item, work = self._leased(work_id, token, now)
            next_state = state or work.state
            if next_state not in transitions.get(work.state, set()):
                raise ValueError("Invalid worker transition")
            updates = {"state": next_state, "updated_at": now, "lease_until": min(
                work.deadline_at, now + timedelta(seconds=self.settings.worker.lease_seconds))}
            if progress is not None:
                if not isinstance(progress, Progress):
                    raise TypeError("Use validated progress")
                if (any(getattr(progress, field) < getattr(work.progress, field) for field in
                        ("fetched", "duplicates_removed", "analyzed", "skipped")) or
                        progress.fetched - progress.duplicates_removed > work.input.max_comments):
                    raise ValueError("Progress cannot shrink or exceed the requested sample")
                updates["progress"] = progress
            if sampling is not None:
                if not isinstance(sampling, Sampling) or sampling.requested_max_comments != work.input.max_comments:
                    raise ValueError("Sampling must match the accepted request")
                updates["sampling"] = sampling
            if checkpoint is not None:
                self._pointer(work, token, checkpoint, "checkpoints")
                updates["checkpoint"] = checkpoint
            updated = work.model_copy(update=updates)
            if self.store.commit([self._work_change(item, updated)]):
                return updated
        raise JobError("SYSTEM_BUSY")

    def _pointer(self, work, token, pointer, kind):
        prefix = f"{self.settings.storage.serving_prefix}/work/{work.work_id}/attempt/{token}/{kind}/"
        if (not isinstance(pointer, ObjectPointer) or not pointer.key.startswith(prefix) or
                pointer.key != prefix + pointer.sha256 + ".json" or pointer.expires_at != work.expires_at or
                pointer.bytes > self.settings.worker.maximum_result_bytes):
            raise ValueError("Only this leased attempt's immutable object pointer is accepted")

    def spend_attempt(self, work_id, token):
        """Commit one allowance before HTTP. Transport failure cannot refund it."""
        for _ in range(8):
            circuit = self.circuit.assert_available()
            now = self.clock()
            item, work = self._leased(work_id, token, now)
            day = quota_day(now)
            old_key = "QUOTA#" + work.quota_day
            old_item = self.store.get(old_key)
            old = dict(old_item.data)
            changes = [self.circuit.condition(circuit)]
            if day != work.quota_day:
                new_key = "QUOTA#" + day
                new_item = self.store.get(new_key)
                budget = self.settings.youtube.working_daily_quota_units
                current = dict(new_item.data) if new_item else {"spent": 0, "reserved": 0, "budget": budget}
                if current["spent"] + current["reserved"] + work.quota_remaining > min(current["budget"], budget):
                    self.telemetry.emit("quota_denied", {"QuotaDeniedCount": 1}, error_code="YOUTUBE_QUOTA_EXHAUSTED", work_id=work.work_id)
                    raise JobError("YOUTUBE_QUOTA_EXHAUSTED")
                old["reserved"] -= work.quota_remaining
                current["reserved"] += work.quota_remaining
                changes.append(self._quota_change(old_key, old_item, old))
            else:
                new_key, new_item, current = old_key, old_item, old
            remaining = work.quota_remaining
            if remaining == 0:
                # A resumed worker may need one extra call, but never a fresh full reservation.
                if current["spent"] + current["reserved"] + 1 > min(
                        current["budget"], self.settings.youtube.working_daily_quota_units):
                    self.telemetry.emit("quota_denied", {"QuotaDeniedCount": 1}, error_code="YOUTUBE_QUOTA_EXHAUSTED", work_id=work.work_id)
                    raise JobError("YOUTUBE_QUOTA_EXHAUSTED")
                current["reserved"] += 1
                remaining = 1
            second_key = f"LIMIT#HTTP#{epoch(now)}"
            limiter = self.store.get(second_key)
            count = limiter.data["count"] if limiter else 0
            if count >= self.settings.youtube.global_http_attempts_per_second:
                raise JobError("RATE_LIMITED")
            current["reserved"] -= 1
            current["spent"] += 1
            updated = work.model_copy(update={"quota_day": day, "quota_remaining": remaining - 1, "updated_at": now})
            changes.extend([self._quota_change(new_key, new_item, current), self._work_change(item, updated),
                            self._change(second_key, limiter, {"count": count + 1}, cleanup_at=epoch(now) + 3600)])
            if self.store.commit(changes):
                self.telemetry.emit("quota_spent", {"YouTubeAttemptUnits": 1}, work_id=work.work_id, attempt=token,
                                    release_id=work.model.release_id)
                return updated
        raise JobError("SYSTEM_BUSY")

    def finish(self, work_id, token, *, state, result=None, failure=None, partial_reason=None):
        if state not in TERMINAL_STATES:
            raise ValueError("Finish requires a terminal state")
        for _ in range(8):
            now = self.clock()
            item, work = self._leased(work_id, token, now)
            if state in {"SUCCEEDED", "PARTIAL"}:
                if work.state != "SAVING":
                    raise ValueError("Publish results only from SAVING")
                self._pointer(work, token, result, "results")
            elif not isinstance(failure, Failure):
                raise ValueError("Failed work needs a validated failure")
            updated = work.model_copy(update={"state": state, "result": result, "failure": failure,
                                              "partial_reason": partial_reason, "updated_at": now,
                                              "fresh_until": min(work.expires_at, now + timedelta(
                                                  seconds=self.settings.cache.freshness_seconds)) if result else None,
                                              "lease_until": None, "terminal_released": True})
            self._validate_terminal(updated)
            if self.store.commit(self._terminal_changes(item, updated)):
                self._terminal_metrics(updated)
                return updated.model_copy(update={"quota_remaining": 0})
        raise JobError("SYSTEM_BUSY")

    def _terminal_metrics(self, work):
        self.telemetry.emit("job_terminal", {"JobSucceededCount": int(work.state == "SUCCEEDED"),
            "JobPartialCount": int(work.state == "PARTIAL"), "JobFailedCount": int(work.state == "FAILED"),
            "AnalyzedCommentCount": work.progress.analyzed, "SkippedCommentCount": work.progress.skipped,
            "JobDurationMillis": max(0.0, (work.updated_at - work.created_at).total_seconds() * 1000)},
            outcome=work.state, work_id=work.work_id, error_code=work.failure.code if work.failure else None,
            attempt=work.lease_token, release_id=work.model.release_id)

    @staticmethod
    def _validate_terminal(work):
        # Reuse the public contract's count/state rules before committing a terminal record.
        Analysis(analysis_id=work.work_id, video_id=work.input.video_id, state=work.state,
                 created_at=work.created_at, updated_at=work.updated_at, deadline_at=work.deadline_at,
                 expires_at=work.expires_at, cache_hit=False, model=work.model, sampling=work.sampling,
                 progress=work.progress, result_available=work.result is not None,
                 partial_reason=work.partial_reason, failure=work.failure)

    def _terminal_changes(self, item, work):
        if item.data["terminal_released"]:
            raise JobError("LEASE_LOST")
        quota_key = "QUOTA#" + work.quota_day
        quota = self.store.get(quota_key)
        q = dict(quota.data)
        q["reserved"] -= work.quota_remaining
        active = self.store.get("LIMIT#GLOBAL#ACTIVE")
        if q["reserved"] < 0 or active.data["count"] <= 0:
            raise RuntimeError("Inconsistent reservation or active counter")
        cleared = work.model_copy(update={"quota_remaining": 0})
        return [self._work_change(item, cleared), self._quota_change(quota_key, quota, q),
                self._change("LIMIT#GLOBAL#ACTIVE", active, {"count": active.data["count"] - 1})]

    def expire(self, work_id):
        for _ in range(8):
            item = self.store.get("WORK#" + str(work_id))
            if item is None or item.data["state"] in TERMINAL_STATES:
                return False
            now = self.clock()
            work = WorkRecord.model_validate(item.data)
            if work.deadline_at > now:
                return False
            expired = work.model_copy(update={"state": "FAILED", "updated_at": now, "lease_until": None,
                                              "failure": Failure(code="JOB_DEADLINE_EXCEEDED",
                                                                 message="Analysis deadline exceeded", retryable=False),
                                              "terminal_released": True})
            if self.store.commit(self._terminal_changes(item, expired)):
                self._terminal_metrics(expired)
                return True
        raise JobError("SYSTEM_BUSY")

    def recover(self, work_id, *, token=None, not_before=None):
        """Requeue once, durably delaying provider retries without keeping a lease."""
        if not_before is not None:
            if not isinstance(not_before, datetime) or not_before.tzinfo is None or not_before.utcoffset() is None:
                raise ValueError("Retry eligibility requires an aware timestamp")
            not_before = not_before.astimezone(timezone.utc)
        for _ in range(8):
            item = self.store.get("WORK#" + str(work_id))
            if item is None:
                return False
            now = self.clock()
            work = WorkRecord.model_validate(item.data)
            if work.state in TERMINAL_STATES or work.deadline_at <= now:
                return self.expire(work_id)
            if work.lease_until is None:
                return False
            if token is not None:
                self._leased(work_id, token, now)
            elif work.lease_until > now:
                return False
            queued = work.model_copy(update={"state": "QUEUED", "lease_until": None, "updated_at": now,
                                             "dispatch_generation": work.dispatch_generation + 1})
            eligible = min(max(now, not_before), work.deadline_at) if not_before is not None else None
            if self.store.commit([self._work_change(item, queued),
                                  self._outbox_change(queued, now, not_before=eligible)]):
                return True
        raise JobError("SYSTEM_BUSY")
