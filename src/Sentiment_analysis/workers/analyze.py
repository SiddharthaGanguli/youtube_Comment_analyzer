"""SQS Lambda entry point; deploy with batch size 1 and ReportBatchItemFailures."""

import logging

from src.Sentiment_analysis.config.worker_runtime import worker_runtime

logger = logging.getLogger(__name__)


def handler(event, context):
    try:
        pipeline = worker_runtime()
    except Exception:
        logger.warning("Analysis worker initialization failed")
        records = event.get("Records") if isinstance(event, dict) else None
        if not isinstance(records, list) or not records or any(
                not isinstance(r, dict) or not isinstance(r.get("messageId"), str) or not r["messageId"] for r in records):
            raise ValueError("Expected identifiable SQS records") from None
        return {"batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in records]}
    return pipeline.handle_sqs(event, context)
