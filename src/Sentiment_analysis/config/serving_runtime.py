"""Lazy wiring for deployed delivery functions. Importing this module is offline."""

import json
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

from src.Sentiment_analysis.components.job_delivery import Dispatcher
from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.integrations.objects import ObjectStore
from src.Sentiment_analysis.pipeline.job_maintenance import Reconciler
from src.Sentiment_analysis.repositories.dynamo import DynamoStore
from src.Sentiment_analysis.repositories.jobs import JobRepository
from src.Sentiment_analysis.repositories.models import ModelRepository


def queue_registry(raw: str, region: str) -> dict[str, str]:
    registry = json.loads(raw)
    if not isinstance(registry, dict) or not registry:
        raise ValueError("Provide a nonempty release-to-SQS-URL registry")
    from src.Sentiment_analysis.entity.analysis import WorkMessage
    from uuid import UUID
    for release, url in registry.items():
        WorkMessage(work_id=UUID(int=0), release_id=release, dispatch_generation=0)
        if not isinstance(url, str):
            raise ValueError("Queue URL must be a string")
        parts = urlsplit(url)
        segments = parts.path.strip("/").split("/")
        if (parts.scheme != "https" or parts.netloc != f"sqs.{region}.amazonaws.com" or
                len(segments) != 2 or len(segments[0]) != 12 or not segments[0].isdigit() or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", segments[1]) or parts.query or parts.fragment):
            raise ValueError("Use the regional AWS SQS URL for each release queue")
    if len(set(registry.values())) != len(registry):
        raise ValueError("Each immutable release needs its own queue")
    return registry


@lru_cache(maxsize=1)
def delivery_runtime():
    settings = ServingConfigurationManager(os.environ.get("SERVING_CONFIG_PATH")).get_serving_config()
    if settings.storage.table_name is None:
        raise ValueError("Deployment must assign storage.table_name")
    registry = queue_registry(os.environ.get("SERVING_QUEUE_REGISTRY_JSON", "{}"), settings.region)
    # Explicit timeouts bound dispatcher/reconciler I/O. Authentication uses the role.
    sdk = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 3})
    session = boto3.Session(region_name=settings.region)
    clock = lambda: datetime.now(timezone.utc)
    jobs = JobRepository(DynamoStore(session.client("dynamodb", config=sdk), settings.storage.table_name),
                         settings, clock=clock)
    objects = ObjectStore(session.client("s3", config=sdk.merge(Config(signature_version="s3v4"))),
                          settings, clock=clock)
    dispatcher = Dispatcher(jobs, session.client("sqs", config=sdk), registry.__getitem__)
    return dispatcher, Reconciler(jobs, dispatcher, objects)


@lru_cache(maxsize=1)
def api_runtime():
    """The API needs DynamoDB and signing credentials, with no SQS/model client."""
    from src.Sentiment_analysis.pipeline.api import APIPipeline
    settings = ServingConfigurationManager(os.environ.get("SERVING_CONFIG_PATH")).get_serving_config()
    if settings.storage.table_name is None:
        raise ValueError("Deployment must assign storage.table_name")
    sdk = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 3})
    session = boto3.Session(region_name=settings.region)
    clock = lambda: datetime.now(timezone.utc)
    store = DynamoStore(session.client("dynamodb", config=sdk), settings.storage.table_name)
    jobs = JobRepository(store, settings, clock=clock)
    objects = ObjectStore(session.client("s3", config=sdk.merge(Config(signature_version="s3v4"))), settings, clock=clock)
    return APIPipeline(settings, jobs, objects, ModelRepository(store))
