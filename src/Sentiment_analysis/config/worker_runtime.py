"""Lazy warm-worker wiring. Importing the entry point never loads ML or AWS."""

import os
from datetime import datetime, timezone
from functools import lru_cache

from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.config.deployment import runtime_settings
from src.Sentiment_analysis.config.worker import WorkerConfigurationManager
from src.Sentiment_analysis.config.youtube import load_youtube_api_key, validate_live_youtube


def build_worker_pipeline(settings, jobs, objects, provider, *, image_code_revision):
    config = WorkerConfigurationManager(settings, image_code_revision=image_code_revision).get_worker_config()
    from src.Sentiment_analysis.services.prediction import PredictionService
    from src.Sentiment_analysis.pipeline.analysis import AnalysisPipeline
    predictor = PredictionService(config.prediction, native_thread_limit=settings.worker.native_thread_limit)
    return AnalysisPipeline(settings, jobs, objects, provider, predictor, config.identity)


@lru_cache(maxsize=1)
def worker_runtime():
    settings = runtime_settings(manager=ServingConfigurationManager)
    fixture_path = os.environ.get("SERVING_FIXTURE_PATH")
    if not settings.storage.table_name:
        raise ValueError("Deployment must assign storage.table_name")
    if fixture_path:
        if settings.environment != "development":
            raise ValueError("Fixture providers are allowed only in development")
        from src.Sentiment_analysis.integrations.fixture_comments import FixtureCommentProvider
        provider = FixtureCommentProvider.from_file(settings, fixture_path)
    else:
        # Reject missing approval/configuration before constructing any AWS
        # clients; this also catches unvalidated model_copy changes.
        validate_live_youtube(settings)
    from src.Sentiment_analysis.integrations.objects import ObjectStore
    from src.Sentiment_analysis.repositories.dynamo import DynamoStore
    from src.Sentiment_analysis.repositories.jobs import JobRepository
    import boto3
    from botocore.config import Config
    sdk = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 3})
    session = boto3.Session(region_name=settings.region)
    if not fixture_path:
        from src.Sentiment_analysis.integrations.youtube import YouTubeCommentProvider
        api_key = load_youtube_api_key(settings, session.client("secretsmanager", config=sdk))
        provider = YouTubeCommentProvider(settings, api_key)
    clock = lambda: datetime.now(timezone.utc)
    jobs = JobRepository(DynamoStore(session.client("dynamodb", config=sdk), settings.storage.table_name), settings, clock=clock)
    objects = ObjectStore(session.client("s3", config=sdk.merge(Config(signature_version="s3v4"))), settings, clock=clock)
    return build_worker_pipeline(settings, jobs, objects, provider,
                                 image_code_revision=os.environ.get("SERVING_IMAGE_CODE_REVISION"))
