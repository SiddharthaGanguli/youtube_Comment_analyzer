"""Measure HTTP outcomes without recording request paths, headers or bodies."""

import re
import time


def route_label(scope):
    method, path = scope["method"], scope["path"]
    if method == "OPTIONS":
        return "OPTIONS"
    exact = {("POST", "/v1/analyses"), ("GET", "/v1/models/current"), ("GET", "/health/live")}
    if (method, path) in exact:
        return f"{method} {path}"
    if re.fullmatch(r"/v1/analyses/[A-Za-z0-9-]{36}", path) and method in {"GET", "DELETE"}:
        return f"{method} /v1/analyses/{{analysis_id}}"
    if re.fullmatch(r"/v1/analyses/[A-Za-z0-9-]{36}/result", path) and method == "GET":
        return "GET /v1/analyses/{analysis_id}/result"
    return "other"


class RequestMetrics:
    def __init__(self, app, *, telemetry, monotonic=None):
        self.app, self.telemetry, self.monotonic = app, telemetry, monotonic or time.monotonic

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, status, interrupted = self.monotonic(), 0, False
        async def measured_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, measured_send)
        except Exception:
            interrupted = True
            raise
        finally:
            state = scope.get("state", {})
            code = state.get("error_code")
            self.telemetry.emit("api_request", {"RequestCount": 1, "RequestErrorCount": int(status >= 400 or interrupted),
                "AuthDeniedCount": int(status in {401, 403}), "RateLimitDeniedCount": int(status == 429),
                "DependencyFailureCount": int(code == "DEPENDENCY_UNAVAILABLE" or interrupted),
                "RequestLatencyMillis": max(0.0, (self.monotonic() - started) * 1000)},
                route=route_label(scope), status_code=status, error_code=code, request_id=state.get("request_id"))
