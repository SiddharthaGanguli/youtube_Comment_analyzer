"""Lambda handler for the minute schedule. No video fetching or model loading."""

from src.Sentiment_analysis.config.serving_runtime import delivery_runtime


def handler(event, context):
    _, reconciler = delivery_runtime()
    counts = reconciler.run()
    if counts["errors"]:
        # Emit an invocation failure so deployment alarms can see recovery problems.
        raise RuntimeError("Scheduled serving maintenance needs retry")
    return counts
