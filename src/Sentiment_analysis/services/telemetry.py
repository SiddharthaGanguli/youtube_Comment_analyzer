"""CloudWatch EMF on stdout. No AWS call, arbitrary message or user dimension."""

import json
import math
import time

from src.Sentiment_analysis.entity.telemetry import TelemetryEvent

NAMESPACE = "CommentLens/Serving"
COUNT_METRICS = frozenset({"RequestCount", "RequestErrorCount", "AuthDeniedCount", "RateLimitDeniedCount",
    "DependencyFailureCount", "SubmissionCount", "NewWorkCount", "CacheHitCount", "ReplayCount", "JoinedWorkCount",
    "QuotaReservedUnits", "YouTubeAttemptUnits", "QuotaDeniedCount", "JobSucceededCount", "JobPartialCount", "JobFailedCount",
    "AnalyzedCommentCount", "SkippedCommentCount", "WorkerDeliveryCount", "DuplicateDeliveryCount", "MisroutedRecordCount",
    "WorkerDependencyFailureCount", "MaintenanceErrorCount", "MaintenanceRecoveredCount",
    "MaintenanceDispatchedCount", "MaintenanceCleanedCount", "DispatchCount", "DispatchFailureCount"})
TIME_METRICS = frozenset({"RequestLatencyMillis", "JobDurationMillis"})


def stdout_document(document):
    print(json.dumps(document, separators=(",", ":"), allow_nan=False), flush=True)


class Telemetry:
    def __init__(self, environment, service, *, sink=None, clock_ms=None):
        if environment not in {"development", "production"} or service not in {"api", "jobs", "worker", "maintenance", "dispatcher"}:
            raise ValueError("Telemetry dimensions must be fixed deployment labels")
        self.environment, self.service = environment, service
        self.sink, self.clock_ms = sink or stdout_document, clock_ms or (lambda: int(time.time() * 1000))

    def emit(self, event, metrics, **fields):
        """Logging is best effort; a sink failure must not change job correctness."""
        try:
            record = TelemetryEvent(event=event, **fields)
            if not isinstance(metrics, dict) or not metrics or not set(metrics) <= COUNT_METRICS | TIME_METRICS:
                return False
            definitions = []
            for name, value in metrics.items():
                if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                    return False
                if name in COUNT_METRICS and type(value) is not int:
                    return False
                definitions.append({"Name": name, "Unit": "Count" if name in COUNT_METRICS else "Milliseconds"})
            timestamp = self.clock_ms()
            if type(timestamp) is not int or timestamp < 0:
                return False
            document = {"_aws": {"Timestamp": timestamp, "CloudWatchMetrics": [{"Namespace": NAMESPACE,
                "Dimensions": [["Environment", "Service"]], "Metrics": definitions}]},
                "Environment": self.environment, "Service": self.service,
                **record.model_dump(mode="json", exclude_none=True), **metrics}
            self.sink(document)
            return True
        except Exception:
            return False
