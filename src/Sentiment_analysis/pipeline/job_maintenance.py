"""Scheduled recovery and cleanup, using bounded index queries rather than scans."""

import logging

from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.jobs import PURGE_DELAY_SECONDS, epoch

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, jobs, dispatcher, objects):
        self.jobs = jobs
        self.dispatcher = dispatcher
        self.objects = objects

    def run(self, *, limit=100):
        now = epoch(self.jobs.clock())
        counts = {"recovered": 0, "dispatched": 0, "cleaned": 0, "errors": 0}
        for item in self.jobs.store.due("work", now, limit=limit):
            try:
                if self.jobs.recover(item.data["work_id"]):
                    counts["recovered"] += 1
            except Exception:
                counts["errors"] += 1
                logger.warning("Serving maintenance operation failed: family=work")
        for item in self.jobs.store.due("outbox", now, limit=limit):
            try:
                if self.dispatcher.dispatch(item.key):
                    counts["dispatched"] += 1
            except Exception:
                counts["errors"] += 1
                logger.warning("Serving maintenance operation failed: family=outbox")
        for indexed in self.jobs.store.due("cleanup", now, cleanup=True, limit=limit):
            try:
                item = self.jobs.store.get(indexed.key)
                # A stale index can point at an item whose expiry was extended.
                if item is None or item.version != indexed.version:
                    continue
                if item.key.startswith("QUOTA#") and item.data["reserved"] != 0:
                    # Keep the audit/counter record until all work released its allowance.
                    continue
                if item.key.startswith("OBJECTS#"):
                    work = self.jobs.get_work(item.data["work_id"])
                    if work is None:
                        raise RuntimeError("Keep work metadata until object cleanup completes")
                    if epoch(work.expires_at) > now:
                        continue
                    self.jobs.expire(work.work_id)
                    self.objects.delete_work_objects(work)
                if item.key.startswith("WORK#"):
                    work = self.jobs.get_work(item.data["work_id"])
                    if epoch(work.expires_at) + PURGE_DELAY_SECONDS > now:
                        continue
                    # An S3 failure cannot erase the only metadata needed for retry.
                    if self.jobs.store.get("OBJECTS#" + str(work.work_id)) is not None:
                        continue
                if self.jobs.store.commit([Change(item.key, item.version, None)]):
                    counts["cleaned"] += 1
            except Exception:
                counts["errors"] += 1
                logger.warning("Serving maintenance operation failed: family=cleanup")
        return counts
