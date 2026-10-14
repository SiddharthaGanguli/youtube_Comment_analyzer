"""Publish the durable outbox. Duplicate sends are expected and safe."""

from src.Sentiment_analysis.entity.analysis import WorkMessage
from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.jobs import PURGE_DELAY_SECONDS, epoch
from src.Sentiment_analysis.services.telemetry import Telemetry


class Dispatcher:
    def __init__(self, jobs, sqs, queue_for_release):
        self.jobs = jobs
        self.sqs = sqs
        self.queue_for_release = queue_for_release
        self.telemetry = Telemetry(jobs.settings.environment, "dispatcher")

    def dispatch(self, key):
        item = self.jobs.store.get(key)
        if item is None or not key.startswith("OUTBOX#") or item.data["sent"]:
            return False
        message = WorkMessage.model_validate(item.data["message"])
        work = self.jobs.get_work(message.work_id)
        if (work is None or work.state != "QUEUED" or work.deadline_at <= self.jobs.clock() or
                work.dispatch_generation != message.dispatch_generation or work.model.release_id != message.release_id):
            # Remove obsolete generations from the due index so they cannot starve
            # newer pending deliveries. Their metadata still waits for cleanup.
            self.jobs.store.commit([Change(item.key, item.version, dict(item.data, sent=True, discarded=True),
                                           cleanup_at=epoch(work.expires_at) + PURGE_DELAY_SECONDS if work else
                                           epoch(self.jobs.clock()) + PURGE_DELAY_SECONDS)])
            return False
        if item.data.get("not_before") is not None and item.data["not_before"] > self.jobs.clock().timestamp():
            # A stream record arrives immediately; a scheduled retry stays
            # pending in the due index until the reconciler reaches its time.
            return False
        # If marking sent fails, leave it pending. Recovery sends the same message again.
        self.sqs.send_message(QueueUrl=self.queue_for_release(message.release_id),
                              MessageBody=message.model_dump_json())
        data = dict(item.data, sent=True, sent_at=epoch(self.jobs.clock()))
        committed = self.jobs.store.commit([Change(item.key, item.version, data,
                                                  cleanup_at=epoch(work.expires_at) + PURGE_DELAY_SECONDS)])
        self.telemetry.emit("dispatch", {"DispatchCount": 1}, outcome="done", work_id=work.work_id)
        return committed

    def handle_stream(self, event):
        failures = []
        for record in event.get("Records", []):
            key = record.get("dynamodb", {}).get("Keys", {}).get("pk", {}).get("S", "")
            if record.get("eventName") not in {"INSERT", "MODIFY"} or not key.startswith("OUTBOX#"):
                continue
            try:
                self.dispatch(key)
            except Exception:
                self.telemetry.emit("dispatch", {"DispatchFailureCount": 1}, outcome="retry")
                failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
        return {"batchItemFailures": failures}
