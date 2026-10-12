"""Private, immutable attempt objects and short-lived authorized downloads."""

import base64
import hashlib
import json
import math
from datetime import timedelta

from botocore.exceptions import ClientError

from src.Sentiment_analysis.entity.analysis import AnalysisResult, ResultDownload, TERMINAL_STATES
from src.Sentiment_analysis.entity.jobs import ObjectPointer
from src.Sentiment_analysis.repositories.jobs import JobError


class ObjectStore:
    def __init__(self, client, settings, *, clock):
        self.client = client
        self.settings = settings
        self.clock = clock

    def _prefix(self, work):
        return f"{self.settings.storage.serving_prefix}/work/{work.work_id}/"

    def _validate_pointer(self, work, pointer):
        prefix = self._prefix(work)
        parts = pointer.key.removeprefix(prefix).split("/")
        if (not pointer.key.startswith(prefix) or len(parts) != 4 or parts[0] != "attempt" or
                not parts[1].isdigit() or int(parts[1]) < 1 or parts[2] not in {"checkpoints", "results"} or
                parts[3] != pointer.sha256 + ".json" or pointer.expires_at != work.expires_at):
            raise ValueError("Object pointer must belong to this work under the serving prefix")

    def _put(self, work, token, kind, value):
        if (work.state in TERMINAL_STATES or work.lease_token != token or
                work.lease_until is None or work.lease_until <= self.clock() or work.deadline_at <= self.clock()):
            raise JobError("LEASE_LOST")
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not data or len(data) > self.settings.worker.maximum_result_bytes:
            raise JobError("RESULT_TOO_LARGE")
        sha = hashlib.sha256(data).hexdigest()
        pointer = ObjectPointer(key=f"{self._prefix(work)}attempt/{token}/{kind}/{sha}.json",
                                sha256=sha, bytes=len(data), expires_at=work.expires_at)
        try:
            self.client.put_object(
                Bucket=self.settings.storage.bucket, Key=pointer.key, Body=data,
                ContentType="application/json", ServerSideEncryption="AES256", IfNoneMatch="*",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),
                Metadata={"sha256": sha, "expires-at": str(int(work.expires_at.timestamp()))})
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            # Retrying the same upload is harmless; a different object is never overwritten.
            self.read(work, pointer)
        return pointer

    def write_checkpoint(self, work, token, payload: dict):
        envelope = {"schema_version": 1, "work_id": str(work.work_id),
                    "release_id": work.model.release_id, "lease_token": token,
                    "expires_at": work.expires_at.isoformat(), "payload": payload}
        return self._put(work, token, "checkpoints", envelope)

    def read_checkpoint(self, work):
        if work.checkpoint is None:
            return None
        envelope = self.read(work, work.checkpoint)
        if (envelope.get("schema_version") != 1 or envelope.get("work_id") != str(work.work_id) or
                envelope.get("release_id") != work.model.release_id or
                str(envelope.get("lease_token")) != work.checkpoint.key.split("/attempt/")[1].split("/")[0] or
                envelope.get("expires_at") != work.expires_at.isoformat() or not isinstance(envelope.get("payload"), dict)):
            raise ValueError("Checkpoint identity or release mismatch")
        return envelope["payload"]

    def write_result(self, work, token, result: AnalysisResult):
        if not isinstance(result, AnalysisResult) or work.state != "SAVING":
            raise ValueError("Upload a validated result only while SAVING")
        if (result.model != work.model or result.video.id != work.input.video_id or
                result.expires_at != work.expires_at or result.sampling != work.sampling or
                not work.created_at <= result.fetched_at <= self.clock() or
                result.duplicates_removed != work.progress.duplicates_removed or
                result.summary.received != work.progress.fetched - work.progress.duplicates_removed or
                result.summary.analyzed != work.progress.analyzed or result.summary.skipped != work.progress.skipped):
            raise ValueError("Result metadata must reconcile with accepted work and progress")
        return self._put(work, token, "results", result.model_dump(mode="json"))

    def read(self, work, pointer):
        self._validate_pointer(work, pointer)
        if pointer.expires_at <= self.clock():
            raise JobError("EXPIRED")
        if pointer.bytes > self.settings.worker.maximum_result_bytes:
            raise JobError("RESULT_TOO_LARGE")
        response = self.client.get_object(Bucket=self.settings.storage.bucket, Key=pointer.key)
        body = response["Body"]
        try:
            data = body.read(pointer.bytes + 1)
        finally:
            body.close()
        if len(data) != pointer.bytes or hashlib.sha256(data).hexdigest() != pointer.sha256:
            raise ValueError("Stored object size or checksum mismatch")
        return json.loads(data)

    def download(self, jobs, owner, analysis_id, *, credential_expires_at=None):
        request, work = jobs.owned(owner, analysis_id)
        if work.state not in {"SUCCEEDED", "PARTIAL"} or work.result is None:
            raise JobError("RESULT_NOT_READY")
        self._validate_pointer(work, work.result)
        if "/results/" not in work.result.key:
            raise ValueError("Only published results can be downloaded")
        now = self.clock()
        expiry = min(request.expires_at, work.expires_at, work.result.expires_at)
        if credential_expires_at is not None:
            expiry = min(expiry, credential_expires_at)
        seconds = min(self.settings.cache.download_url_seconds, math.floor((expiry - now).total_seconds()))
        if seconds < 1:
            raise JobError("EXPIRED")
        url = self.client.generate_presigned_url("get_object", Params={
            "Bucket": self.settings.storage.bucket, "Key": work.result.key,
        }, ExpiresIn=seconds)
        return ResultDownload(url=url, expires_at=now + timedelta(seconds=seconds),
                              sha256=work.result.sha256, bytes=work.result.bytes,
                              content_type="application/json", schema_version=1)

    def delete_work_objects(self, work):
        """Include unpublished attempts; don't rely on just the final pointer."""
        prefix = self._prefix(work)
        for page in self.client.get_paginator("list_object_versions").paginate(
                Bucket=self.settings.storage.bucket, Prefix=prefix):
            # The existing dataset bucket may use versioning. Deleting only the
            # current key would retain comment text in noncurrent versions.
            keys = [{"Key": row["Key"], "VersionId": row["VersionId"]}
                    for row in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]]
            if keys:
                # Versions and delete markers together can exceed one delete batch.
                for start in range(0, len(keys), 1000):
                    response = self.client.delete_objects(Bucket=self.settings.storage.bucket,
                                                         Delete={"Objects": keys[start:start + 1000], "Quiet": True})
                    if response.get("Errors"):
                        raise RuntimeError("Serving object cleanup failed; keep the work record for retry")
