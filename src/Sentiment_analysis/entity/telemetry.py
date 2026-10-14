"""Allowlisted log fields; rejected input and identity data have no field here."""

from typing import Annotated, Literal

from pydantic import Field

from src.Sentiment_analysis.entity.analysis import UUIDValue
from src.Sentiment_analysis.entity.serving import ReleaseId, ServingEntity

EventName = Literal["api_request", "admission", "quota_spent", "quota_denied", "job_terminal",
                    "worker_delivery", "worker_dependency_error", "maintenance", "dispatch", "provider_circuit"]
RouteName = Literal["none", "other", "POST /v1/analyses", "GET /v1/analyses/{analysis_id}",
                    "DELETE /v1/analyses/{analysis_id}", "GET /v1/analyses/{analysis_id}/result",
                    "GET /v1/models/current", "GET /health/live", "OPTIONS"]
ErrorCode = Literal["UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "EXPIRED", "INVALID_INPUT", "REQUEST_TOO_LARGE",
                    "IDEMPOTENCY_CONFLICT", "RESULT_NOT_READY", "ANALYSIS_FAILED", "RATE_LIMITED", "ACTIVE_JOB_LIMIT",
                    "SYSTEM_BUSY", "YOUTUBE_QUOTA_EXHAUSTED", "LIVE_ANALYSIS_DISABLED", "DEPENDENCY_UNAVAILABLE",
                    "RELEASE_MISMATCH", "WORKER_ATTEMPTS_EXHAUSTED", "VIDEO_NOT_FOUND", "VIDEO_NOT_PUBLIC",
                    "COMMENTS_DISABLED", "INVALID_PROVIDER_RESPONSE", "PAGE_LIMIT_REACHED", "FETCH_RETRIES_EXHAUSTED",
                    "RESULT_TOO_LARGE", "INVALID_CHECKPOINT", "INVALID_PREDICTION", "JOB_DEADLINE_EXCEEDED",
                    "PROVIDER_UNAVAILABLE", "PROVIDER_CREDENTIALS_INVALID", "PROVIDER_ACCESS_DENIED"]


class TelemetryEvent(ServingEntity):
    event: EventName
    route: RouteName = "none"
    outcome: Literal["accepted", "replay", "cache_hit", "joined", "SUCCEEDED", "PARTIAL", "FAILED",
                     "duplicate", "misrouted", "retry", "requeued", "done"] | None = None
    error_code: ErrorCode | None = None
    request_id: UUIDValue | None = None
    work_id: UUIDValue | None = None
    release_id: ReleaseId | None = None
    status_code: Annotated[int, Field(ge=0, le=599)] | None = None
    attempt: Annotated[int, Field(ge=0)] | None = None
