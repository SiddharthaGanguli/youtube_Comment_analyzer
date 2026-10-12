"""Shared in-process AWS fixture. Only synthetic values and fake credentials."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from moto import mock_aws

from src.Sentiment_analysis.components.job_delivery import Dispatcher
from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.entity.analysis import AnalysisInput, AnalysisResult, ModelReference, Progress, WorkMessage
from src.Sentiment_analysis.integrations.objects import ObjectStore
from src.Sentiment_analysis.pipeline.job_maintenance import Reconciler
from src.Sentiment_analysis.repositories.dynamo import DynamoStore
from src.Sentiment_analysis.repositories.jobs import JobError, JobRepository
from tests.serving.fixtures import MODEL, VIDEO_ID, result

ROOT = Path(__file__).resolve().parents[2]


class StorageFixture:
    """Mixin for unittest cases; cleanup is registered even when setup later fails."""

    def setUp(self):
        self.aws = mock_aws(config={"core": {"service_whitelist": ["dynamodb", "s3", "sqs"]}})
        self.aws.start()
        self.addCleanup(self.aws.stop)
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
        self.ddb = session.client("dynamodb")
        self.ddb.create_table(
            TableName="serving-test", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": kind} for name, kind in (
                ("pk", "S"), ("due_pk", "S"), ("due_at", "N"), ("cleanup_pk", "S"),
                ("cleanup_at", "N"), ("owner_pk", "S"), ("created_at", "N"))],
            GlobalSecondaryIndexes=[{
                "IndexName": name, "KeySchema": [{"AttributeName": partition, "KeyType": "HASH"},
                                                   {"AttributeName": sort, "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            } for name, partition, sort in (
                ("DueIndex", "due_pk", "due_at"), ("CleanupIndex", "cleanup_pk", "cleanup_at"),
                ("OwnerIndex", "owner_pk", "created_at"))])
        self.config = ServingConfigurationManager(project_root=ROOT).get_serving_config()
        self.config = self.config.model_copy(update={"model": self.config.model.model_copy(update={
            "sha256": MODEL["sha256"], "preprocessing_sha256": MODEL["preprocessing_sha256"],
            "release_id": MODEL["release_id"]})})
        self.now = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
        self.store = DynamoStore(self.ddb, "serving-test")
        self.jobs = JobRepository(self.store, self.config, clock=lambda: self.now)
        self.s3 = session.client("s3", config=Config(signature_version="s3v4"))
        self.s3.create_bucket(Bucket=self.config.storage.bucket)
        self.objects = ObjectStore(self.s3, self.config, clock=lambda: self.now)
        self.sqs = session.client("sqs")
        self.queue = self.sqs.create_queue(QueueName="fixture-release")["QueueUrl"]
        self.dispatch = Dispatcher(self.jobs, self.sqs, lambda release: self.queue)
        self.reconcile = Reconciler(self.jobs, self.dispatch, self.objects)
        self.model = ModelReference.model_validate(MODEL)
        self.input = AnalysisInput(video_url=f"https://youtu.be/{VIDEO_ID}")

    def admit(self, owner="alice", key=None, input=None):
        return self.jobs.admit(owner, key or uuid4(), input or self.input, self.model)

    def message(self, work):
        return WorkMessage(work_id=work.work_id, release_id=work.model.release_id,
                           dispatch_generation=work.dispatch_generation)

    def claim(self, work):
        return self.jobs.claim(self.message(work))

    def error(self, code, callback):
        with self.assertRaises(JobError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def complete(self, work, *, empty=True):
        leased = self.claim(work)
        data = result(empty)
        data["model"] = leased.model.model_dump(mode="json")
        data["fetched_at"] = self.now.isoformat()
        data["expires_at"] = leased.expires_at.isoformat()
        data["sampling"]["requested_max_comments"] = leased.input.max_comments
        result_entity = AnalysisResult.model_validate(data)
        count = result_entity.summary.received
        progress = Progress(fetched=count, duplicates_removed=0, analyzed=count, skipped=0)
        leased = self.jobs.heartbeat(work.work_id, leased.lease_token, state="INFERENCING",
                                     progress=progress, sampling=result_entity.sampling)
        leased = self.jobs.heartbeat(work.work_id, leased.lease_token, state="SAVING")
        pointer = self.objects.write_result(leased, leased.lease_token, result_entity)
        self.jobs.finish(work.work_id, leased.lease_token, state=result_entity.state, result=pointer)
        return self.jobs.get_work(work.work_id)
