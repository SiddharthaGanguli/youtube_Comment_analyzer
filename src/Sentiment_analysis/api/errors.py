"""One safe error envelope. Never serialize exceptions or rejected input values."""

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.responses import JSONResponse

from src.Sentiment_analysis.entity.analysis import APIError, ErrorEnvelope
from src.Sentiment_analysis.repositories.jobs import PACIFIC

ERRORS = {
    "UNAUTHORIZED": (401, "A valid access token is required.", False),
    "FORBIDDEN": (403, "The access token does not allow this operation.", False),
    "NOT_FOUND": (404, "Analysis not found.", False),
    "EXPIRED": (410, "This analysis is expired or has been removed.", False),
    "INVALID_INPUT": (422, "Check the request fields, video URL and comment limit.", False),
    "REQUEST_TOO_LARGE": (413, "The request body exceeds the allowed size.", False),
    "IDEMPOTENCY_CONFLICT": (409, "This retry key was already used for a different request.", False),
    "RESULT_NOT_READY": (409, "The analysis result is not ready yet.", True),
    "ANALYSIS_FAILED": (409, "The analysis failed; read its status for the reason.", False),
    "RATE_LIMITED": (429, "The request rate limit has been reached.", True),
    "ACTIVE_JOB_LIMIT": (429, "Your active analysis limit has been reached.", True),
    "SYSTEM_BUSY": (503, "The service is busy; retry after the suggested delay.", True),
    "YOUTUBE_QUOTA_EXHAUSTED": (503, "The daily YouTube allowance is exhausted.", True),
    "LIVE_ANALYSIS_DISABLED": (503, "Live analysis is not enabled yet.", False),
    "DEPENDENCY_UNAVAILABLE": (503, "A required service is temporarily unavailable.", True),
}


def error_response(code, scope, now=None):
    now = now or datetime.now(timezone.utc)
    code = code if code in ERRORS else "DEPENDENCY_UNAVAILABLE"
    status, message, retryable = ERRORS[code]
    request_id = scope.get("state", {}).get("request_id") or str(uuid4())
    retry, reset = None, None
    if code == "RATE_LIMITED":
        retry = 60 - int(now.timestamp()) % 60
    elif code in {"RESULT_NOT_READY", "ACTIVE_JOB_LIMIT", "DEPENDENCY_UNAVAILABLE"}:
        retry = 5
    elif code == "SYSTEM_BUSY":
        retry = 10
    elif code == "YOUTUBE_QUOTA_EXHAUSTED":
        reset = (now.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=1)).astimezone(timezone.utc)
        retry = max(1, math.ceil((reset - now).total_seconds()))
    envelope = ErrorEnvelope(error=APIError(code=code, message=message, retryable=retryable,
                                           request_id=request_id, retry_after_seconds=retry, reset_at=reset))
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    if retry:
        headers["Retry-After"] = str(retry)
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status, headers=headers)
