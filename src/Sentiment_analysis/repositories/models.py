"""Immutable release metadata and an atomic current pointer. No model deserialization."""

from src.Sentiment_analysis.entity.api import ReleaseManifest
from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.jobs import JobError


class ModelRepository:
    def __init__(self, store):
        self.store = store

    def register(self, manifest: ReleaseManifest):
        # Also check programmatically copied manifests before persisting them.
        manifest = ReleaseManifest.model_validate(manifest.model_dump(mode="json"))
        key = "MODEL#" + manifest.details.model.release_id
        value = manifest.model_dump(mode="json")
        for _ in range(8):
            existing = self.store.get(key)
            if existing:
                if existing.data != value:
                    raise ValueError("An existing release manifest cannot be changed")
                return
            if self.store.commit([Change(key, None, value)]):
                return
        raise JobError("SYSTEM_BUSY")

    def promote(self, release_id: str, *, expected_release_id: str | None):
        """For the later release workflow, after verifying its worker/queue readiness."""
        release = self.store.get("MODEL#" + release_id)
        if release is None:
            raise ValueError("Register and verify the immutable manifest before promotion")
        ReleaseManifest.model_validate(release.data)
        current = self.store.get("MODEL#CURRENT")
        if (current.data["release_id"] if current else None) != expected_release_id:
            raise ValueError("The current release changed; re-read before promotion")
        if not self.store.commit([Change(release.key, release.version, None, check_only=True),
                                  Change("MODEL#CURRENT", current.version if current else None,
                                         {"release_id": release_id})]):
            raise JobError("SYSTEM_BUSY")

    def current(self) -> ReleaseManifest:
        current = self.store.get("MODEL#CURRENT")
        release = self.store.get("MODEL#" + current.data["release_id"]) if current else None
        if release is None:
            raise JobError("DEPENDENCY_UNAVAILABLE")
        return ReleaseManifest.model_validate(release.data)
