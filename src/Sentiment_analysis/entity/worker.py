"""Bounded provider inputs and resumable worker state. These stay private."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, model_validator

from src.Sentiment_analysis.entity.analysis import (
    Comment, ModelReference, NonNegativeInt, Progress, UtcTimestamp, Video,
)
from src.Sentiment_analysis.entity.api import release_fingerprint
from src.Sentiment_analysis.entity.entity import ModelPredictionConfig
from src.Sentiment_analysis.entity.serving import ServingEntity, ServingSettings


class WorkerIdentity(ServingEntity):
    code_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    model: ModelReference

    @model_validator(mode="after")
    def binding(self):
        if self.model.release_id != release_fingerprint(self.model, self.code_revision):
            raise ValueError("Worker release does not bind its model and image code revision")
        return self


@dataclass(frozen=True)
class WorkerConfig:
    serving: ServingSettings
    prediction: ModelPredictionConfig
    identity: WorkerIdentity


class ProviderComment(ServingEntity):
    id: Annotated[str, Field(min_length=1, max_length=256)]
    author: Annotated[str, Field(max_length=1000)] | None
    published_at: UtcTimestamp
    likes: NonNegativeInt
    text: str | None = Field(repr=False)


class CommentPage(ServingEntity):
    comments: Annotated[tuple[ProviderComment, ...], Field(max_length=100)]
    next_page_token: Annotated[str, Field(min_length=1, max_length=2048)] | None


class CapturedComment(ProviderComment):
    text: Annotated[str, Field(max_length=5000)] | None = Field(repr=False)
    text_too_long: bool = False

    @model_validator(mode="after")
    def discard_large_text(self):
        if self.text_too_long and self.text is not None:
            raise ValueError("Oversized text cannot be kept in a checkpoint")
        return self


class WorkerCheckpoint(ServingEntity):
    schema_version: Literal[1] = 1
    video: Video | None = None
    fetched_at: UtcTimestamp | None = None
    fetched: Annotated[int, Field(ge=0, le=1000)] = 0
    duplicates_removed: NonNegativeInt = 0
    pending: Annotated[tuple[CapturedComment, ...], Field(max_length=1000)] = ()
    predictions: Annotated[tuple[Comment, ...], Field(max_length=1000)] = ()
    next_page_token: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    page_tokens: Annotated[tuple[str, ...], Field(max_length=1000)] = ()
    call_attempts: Annotated[int, Field(ge=0, le=3)] = 0
    fetch_complete: bool = False
    has_more: bool | None = None
    partial_reason: str | None = None

    @model_validator(mode="after")
    def reconcile(self):
        rows = (*self.predictions, *self.pending)
        if self.fetched != len(rows) + self.duplicates_removed or len({r.id for r in rows}) != len(rows):
            raise ValueError("Checkpoint counts or comment identities do not reconcile")
        if [r.index for r in self.predictions] != list(range(len(self.predictions))):
            raise ValueError("Checkpoint predictions must retain original contiguous indices")
        if (self.video is None) != (self.fetched_at is None):
            raise ValueError("Fetched metadata needs its UTC fetch timestamp")
        if (self.video is None and (rows or self.page_tokens or self.fetch_complete) or
                self.predictions and not self.fetch_complete or
                len(set(self.page_tokens)) != len(self.page_tokens)):
            raise ValueError("Checkpoint phase or page history is invalid")
        if self.fetch_complete != (self.has_more is not None) or self.partial_reason and not self.fetch_complete:
            raise ValueError("Finished fetches must record sampling completeness")
        return self

    @property
    def progress(self):
        analyzed = sum(row.status == "analyzed" for row in self.predictions)
        return Progress(fetched=self.fetched, duplicates_removed=self.duplicates_removed,
                        analyzed=analyzed, skipped=len(self.predictions) - analyzed)
