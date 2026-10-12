"""Lambda handler for the table stream; enable ReportBatchItemFailures in CDK."""

from src.Sentiment_analysis.config.serving_runtime import delivery_runtime


def handler(event, context):
    dispatcher, _ = delivery_runtime()
    return dispatcher.handle_stream(event)
