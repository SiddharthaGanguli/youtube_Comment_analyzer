"""Internal persistence entities. None of these records is a public API response."""

from typing import Annotated
from datetime import date

from pydantic import Field, field_validator, model_validator

from src.Sentiment_analysis.entity.analysis import (
    AnalysisInput, Failure, JobState, ModelReference, Progress, Sampling,
    UUIDValue, UtcTimestamp,
)
from src.Sentiment_analysis.entity.serving import NonNegativeInt, ServingEntity, Sha256


class ObjectPointer(ServingEntity):
    key: str
    sha256: Sha256
    bytes: Annotated[int, Field(ge=1, le=10485760)]
    expires_at: UtcTimestamp


class WorkRecord(ServingEntity):
    work_id: UUIDValue
    input: AnalysisInput
    model: ModelReference
    cache_key: Sha256
    state: JobState
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    deadline_at: UtcTimestamp
    expires_at: UtcTimestamp
    lease_until: UtcTimestamp | None
    lease_token: NonNegativeInt
    dispatch_generation: NonNegativeInt
    quota_day: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    quota_remaining: NonNegativeInt
    terminal_released: bool
    progress: Progress
    sampling: Sampling
    checkpoint: ObjectPointer | None
    result: ObjectPointer | None
    fresh_until: UtcTimestamp | None
    partial_reason: str | None
    failure: Failure | None

    @field_validator("quota_day")
    @classmethod
    def validate_day(cls, value):
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_work(self):
        terminal = self.state in {"SUCCEEDED", "PARTIAL", "FAILED"}
        if (self.terminal_released != terminal or (terminal and self.quota_remaining != 0) or
                ((terminal or self.state == "QUEUED") and self.lease_until is not None) or
                (not terminal and self.state != "QUEUED" and self.lease_until is None)):
            raise ValueError("Work state, reservation release and lease disagree")
        if (self.created_at > self.updated_at or not self.created_at < self.deadline_at < self.expires_at or
                self.sampling.requested_max_comments != self.input.max_comments or
                self.progress.fetched - self.progress.duplicates_removed > self.input.max_comments):
            raise ValueError("Work timing or sample bounds are inconsistent")
        has_result = self.state in {"SUCCEEDED", "PARTIAL"}
        if ((self.result is not None) != has_result or (self.fresh_until is not None) != has_result or
                (self.failure is not None) != (self.state == "FAILED") or
                (self.partial_reason is not None) != (self.state == "PARTIAL")):
            raise ValueError("Terminal work metadata disagrees with its state")
        return self


class OwnedRequest(ServingEntity):
    analysis_id: UUIDValue
    owner: Annotated[str, Field(min_length=1, max_length=256, repr=False)]
    work_id: UUIDValue
    created_at: UtcTimestamp
    expires_at: UtcTimestamp
    cache_hit: bool
    deleted: bool = False


class Admission(ServingEntity):
    request: OwnedRequest
    work: WorkRecord
    replay: bool
