"""Request, job-status and result entities for docs/backend-contract.yaml."""

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Literal, get_args
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BeforeValidator, Field, field_validator, model_serializer, model_validator

from src.Sentiment_analysis.entity.serving import (
    NonEmptyText, NonNegativeInt, Probability, ReleaseId, ServingEntity, Sha256,
)

JobState = Literal["QUEUED", "FETCHING", "INFERENCING", "SAVING", "SUCCEEDED", "PARTIAL", "FAILED"]
Sentiment = Literal["Negative", "Neutral", "Positive"]
SkipReason = Literal["empty_text", "no_known_features", "text_too_long"]
TERMINAL_STATES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED"})
LABELS = {0: "Negative", 1: "Neutral", 2: "Positive"}
CONFIDENCE_DEFINITION = "Highest class score, not calibrated correctness probability"
UUIDValue = Annotated[UUID, Field(strict=False)]


def _timestamp_input(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("Use an ISO timestamp or an aware datetime, not an epoch number")
    return value


UtcTimestamp = Annotated[AwareDatetime, Field(strict=False), BeforeValidator(_timestamp_input),
                         AfterValidator(lambda value: value.astimezone(timezone.utc))]


def canonical_video_url(value: str) -> str:
    """Extract a video ID; callers later construct fixed Google API URLs from it."""
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("Video URL cannot contain whitespace or control characters")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Invalid video URL port") from None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or port not in {None, 443}:
        raise ValueError("Use an HTTPS YouTube URL without credentials or a custom port")
    video_id = None
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parts.path)
        video_id = match[1] if match else None
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts.path == "/watch":
            values = parse_qs(parts.query).get("v", [])
            video_id = values[0] if len(values) == 1 else None
        else:
            match = re.fullmatch(r"/(?:shorts|live)/([A-Za-z0-9_-]{11})/?", parts.path)
            video_id = match[1] if match else None
    if video_id is None or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Use a supported watch, shorts, live or youtu.be video URL")
    return f"https://www.youtube.com/watch?v={video_id}"


class AnalysisInput(ServingEntity):
    video_url: Annotated[str, Field(max_length=1024)]
    max_comments: Annotated[int, Field(ge=1, le=1000)] = 1000

    @field_validator("video_url")
    @classmethod
    def validate_url(cls, value):
        return canonical_video_url(value)

    @property
    def video_id(self) -> str:
        return self.video_url.rsplit("=", 1)[1]


class ModelReference(ServingEntity):
    release_id: ReleaseId
    sha256: Sha256
    preprocessing_sha256: Sha256
    review_confidence_threshold: Probability
    result_schema_version: Literal[1]


class Sampling(ServingEntity):
    scope: Literal["public_top_level_comments"]
    order: Literal["time"]
    include_replies: Literal[False]
    requested_max_comments: Annotated[int, Field(ge=1, le=1000)]
    has_more: bool | None
    sample_start: UtcTimestamp | None
    sample_end: UtcTimestamp | None

    @model_validator(mode="after")
    def validate_period(self):
        if (self.sample_start is None) != (self.sample_end is None):
            raise ValueError("Sample start and end must both be present or both be null")
        if self.sample_start is not None and self.sample_start > self.sample_end:
            raise ValueError("Sample period is reversed")
        return self


class Progress(ServingEntity):
    fetched: NonNegativeInt
    duplicates_removed: NonNegativeInt
    analyzed: NonNegativeInt
    skipped: NonNegativeInt

    @model_validator(mode="after")
    def validate_progress(self):
        if self.duplicates_removed > self.fetched or self.analyzed + self.skipped > self.fetched - self.duplicates_removed:
            raise ValueError("Progress counts exceed the unique fetched sample")
        return self


class Failure(ServingEntity):
    code: NonEmptyText
    message: NonEmptyText
    retryable: bool


class Analysis(ServingEntity):
    analysis_id: UUIDValue
    video_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]
    state: JobState
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    deadline_at: UtcTimestamp
    expires_at: UtcTimestamp
    cache_hit: bool
    model: ModelReference
    sampling: Sampling
    progress: Progress
    result_available: bool
    partial_reason: NonEmptyText | None
    failure: Failure | None

    @model_validator(mode="after")
    def validate_status(self):
        available = self.state in {"SUCCEEDED", "PARTIAL"}
        if self.result_available != available:
            raise ValueError("Only successful or partial analyses have a result")
        if (self.state == "FAILED") != (self.failure is not None):
            raise ValueError("A failed analysis requires a failure; other states cannot contain one")
        if (self.state == "PARTIAL") != (self.partial_reason is not None):
            raise ValueError("Only PARTIAL analyses require a partial reason")
        if self.cache_hit and not available:
            raise ValueError("A completed cache hit must have a result")
        if not self.created_at <= self.updated_at < self.expires_at or self.deadline_at >= self.expires_at:
            raise ValueError("Analysis timestamps do not fit inside retention")
        if self.state not in TERMINAL_STATES and self.deadline_at <= self.created_at:
            raise ValueError("A running analysis needs a future deadline after creation")
        unique = self.progress.fetched - self.progress.duplicates_removed
        if unique > self.sampling.requested_max_comments:
            raise ValueError("Progress exceeds the requested sample size")
        if available and self.progress.analyzed + self.progress.skipped != unique:
            raise ValueError("Completed analysis counts must reconcile")
        return self


class ResultDownload(ServingEntity):
    url: NonEmptyText = Field(repr=False)
    expires_at: UtcTimestamp
    sha256: Sha256
    bytes: Annotated[int, Field(ge=1, le=10485760)]
    content_type: Literal["application/json"]
    schema_version: Literal[1]

    @field_validator("url")
    @classmethod
    def validate_https(cls, value):
        parts = urlsplit(value)
        try:
            parts.port
        except ValueError:
            raise ValueError("Invalid download URL port") from None
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
                or parts.fragment or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError("Result downloads require an HTTPS URL without credentials")
        return value


class Video(ServingEntity):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]
    url: str
    title: str
    channel_title: str
    published_at: UtcTimestamp
    total_comment_count: NonNegativeInt | None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return canonical_video_url(value)

    @model_validator(mode="after")
    def validate_identity(self):
        if self.url.rsplit("=", 1)[1] != self.id:
            raise ValueError("Video URL and ID disagree")
        return self


class SentimentCounts(ServingEntity):
    Negative: NonNegativeInt
    Neutral: NonNegativeInt
    Positive: NonNegativeInt


class SkipCounts(ServingEntity):
    empty_text: NonNegativeInt
    no_known_features: NonNegativeInt
    text_too_long: NonNegativeInt


class Summary(ServingEntity):
    received: Annotated[int, Field(ge=0, le=1000)]
    analyzed: Annotated[int, Field(ge=0, le=1000)]
    skipped: Annotated[int, Field(ge=0, le=1000)]
    coverage: Probability
    sentiment_counts: SentimentCounts
    skipped_by_reason: SkipCounts
    needs_review: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def validate_counts(self):
        if self.received != self.analyzed + self.skipped:
            raise ValueError("Received must equal analyzed plus skipped")
        if sum(self.sentiment_counts.model_dump().values()) != self.analyzed:
            raise ValueError("Sentiment counts must sum to analyzed")
        if sum(self.skipped_by_reason.model_dump().values()) != self.skipped or self.needs_review > self.analyzed:
            raise ValueError("Skip or review totals are inconsistent")
        coverage = self.analyzed / self.received if self.received else 0.0
        if not math.isclose(self.coverage, coverage, abs_tol=1e-9):
            raise ValueError("Coverage must describe analyzed / unique received")
        return self


class ClassProbabilities(ServingEntity):
    Negative: Probability
    Neutral: Probability
    Positive: Probability

    @model_validator(mode="after")
    def validate_sum(self):
        if not math.isclose(sum(self.model_dump().values()), 1.0, abs_tol=1e-7):
            raise ValueError("Class scores must sum to one")
        return self


class Comment(ServingEntity):
    index: Annotated[int, Field(ge=0, le=999)]
    id: NonEmptyText
    author: str | None
    published_at: UtcTimestamp
    likes: NonNegativeInt
    text: Annotated[str, Field(max_length=5000)] | None
    normalized_text: str | None
    status: Literal["analyzed", "skipped"]
    sentiment: Sentiment | None
    sentiment_label: Literal[0, 1, 2] | None
    confidence: Probability | None
    class_probabilities: ClassProbabilities | None
    needs_review: bool
    skipped_reason: SkipReason | None

    @model_validator(mode="after")
    def validate_prediction(self):
        predictions = (self.sentiment, self.sentiment_label, self.confidence, self.class_probabilities)
        if self.status == "skipped":
            if any(value is not None for value in predictions) or self.skipped_reason is None or self.needs_review:
                raise ValueError("Skipped comments cannot contain predictions or review flags")
            if self.skipped_reason == "text_too_long" and (self.text is not None or self.normalized_text is not None):
                raise ValueError("Oversized text must not be retained in the result")
            if self.skipped_reason == "empty_text" and self.normalized_text:
                raise ValueError("Empty-text skips cannot have nonempty normalized text")
            if self.skipped_reason == "no_known_features" and not self.normalized_text:
                raise ValueError("Unknown-feature skips require nonempty normalized text")
        else:
            if any(value is None for value in predictions) or self.skipped_reason is not None or not self.normalized_text or not self.text:
                raise ValueError("Analyzed comments require a complete prediction and normalized text")
            if LABELS[self.sentiment_label] != self.sentiment:
                raise ValueError("Numeric and text sentiment labels disagree")
            scores = self.class_probabilities.model_dump()
            if (not math.isclose(self.confidence, max(scores.values()), abs_tol=1e-7)
                    or not math.isclose(scores[self.sentiment], self.confidence, abs_tol=1e-7)):
                raise ValueError("Confidence and selected sentiment must match the maximum class score")
        return self


class AnalysisResult(ServingEntity):
    schema_version: Literal[1]
    state: Literal["SUCCEEDED", "PARTIAL"]
    fetched_at: UtcTimestamp
    expires_at: UtcTimestamp
    video: Video
    sampling: Sampling
    duplicates_removed: NonNegativeInt
    model: ModelReference
    confidence_definition: Literal[CONFIDENCE_DEFINITION]
    summary: Summary
    partial_reason: NonEmptyText | None
    comments: Annotated[tuple[Comment, ...], Field(max_length=1000)]

    @model_validator(mode="after")
    def validate_result(self):
        if self.expires_at <= self.fetched_at:
            raise ValueError("Result must expire after fetching")
        if (self.state == "PARTIAL") != (self.partial_reason is not None):
            raise ValueError("Only PARTIAL results require a partial reason")
        if len(self.comments) != self.summary.received or len(self.comments) > self.sampling.requested_max_comments:
            raise ValueError("Result rows do not match the received/requested sample size")
        if [row.index for row in self.comments] != list(range(len(self.comments))):
            raise ValueError("Comment indices must preserve contiguous input order")
        if len({row.id for row in self.comments}) != len(self.comments):
            raise ValueError("Repeated comment IDs must be removed before assembling results")
        analyzed = [row for row in self.comments if row.status == "analyzed"]
        counts = Counter(row.sentiment for row in analyzed)
        skips = Counter(row.skipped_reason for row in self.comments if row.status == "skipped")
        if (self.summary.analyzed != len(analyzed)
                or self.summary.sentiment_counts.model_dump() != {label: counts[label] for label in LABELS.values()}
                or self.summary.skipped_by_reason.model_dump() != {reason: skips[reason] for reason in get_args(SkipReason)}
                or self.summary.needs_review != sum(row.needs_review for row in analyzed)):
            raise ValueError("Summary counts must match the actual comment rows")
        if any(row.needs_review != (row.confidence < self.model.review_confidence_threshold) for row in analyzed):
            raise ValueError("Review flags must use this result's model threshold")
        period = (min(row.published_at for row in self.comments), max(row.published_at for row in self.comments)) if self.comments else (None, None)
        if (self.sampling.sample_start, self.sampling.sample_end) != period:
            raise ValueError("Sample period must match the returned comment publication times")
        return self


class ModelLabels(ServingEntity):
    negative: Literal["Negative"] = Field(alias="0")
    neutral: Literal["Neutral"] = Field(alias="1")
    positive: Literal["Positive"] = Field(alias="2")


class Evaluation(ServingEntity):
    test_accuracy: Probability
    test_macro_f1: Probability
    test_rows: Annotated[int, Field(ge=1)]
    labels_human_confirmed: bool
    context: NonEmptyText


class ModelDetails(ServingEntity):
    model: ModelReference
    type: Literal["TF-IDF + LogisticRegression"]
    labels: ModelLabels
    confidence_definition: Literal[CONFIDENCE_DEFINITION]
    evaluation: Evaluation


class APIError(ServingEntity):
    code: Literal["UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "EXPIRED", "INVALID_INPUT",
                  "REQUEST_TOO_LARGE", "IDEMPOTENCY_CONFLICT", "RESULT_NOT_READY", "ANALYSIS_FAILED",
                  "RATE_LIMITED", "ACTIVE_JOB_LIMIT", "SYSTEM_BUSY", "YOUTUBE_QUOTA_EXHAUSTED",
                  "LIVE_ANALYSIS_DISABLED", "DEPENDENCY_UNAVAILABLE"]
    message: NonEmptyText
    retryable: bool
    request_id: NonEmptyText
    retry_after_seconds: Annotated[int, Field(ge=1)] | None = None
    reset_at: UtcTimestamp | None = None

    @model_serializer(mode="wrap")
    def omit_absent_retry_fields(self, handler):
        data = handler(self)
        for name in ("retry_after_seconds", "reset_at"):
            if data.get(name) is None:
                data.pop(name, None)
        return data

    @model_validator(mode="after")
    def quota_reset(self):
        if self.code == "YOUTUBE_QUOTA_EXHAUSTED" and (self.reset_at is None or not self.retryable):
            raise ValueError("Quota exhaustion must supply a retryable reset time")
        return self


class ErrorEnvelope(ServingEntity):
    error: APIError


class WorkMessage(ServingEntity):
    """Small internal queue message; no comments, owners or credentials."""
    work_id: UUIDValue
    release_id: ReleaseId
    dispatch_generation: NonNegativeInt
