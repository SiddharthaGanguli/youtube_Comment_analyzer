"""Typed backend settings, separate from the offline training configuration."""

import math
import re
from decimal import Decimal
from pathlib import Path
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ReleaseId = Annotated[str, Field(pattern=r"^sr_[a-f0-9]{64}$")]
NonEmptyText = Annotated[str, Field(min_length=1)]


class ServingEntity(BaseModel):
    """Reject unknown fields/coercion and keep validated objects immutable."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              validate_default=True, allow_inf_nan=False,
                              hide_input_in_errors=True, serialize_by_alias=True)

    @field_validator("*", mode="before")
    @classmethod
    def validate_collections_and_literals(cls, value, info: ValidationInfo):
        annotation = cls.model_fields[info.field_name].annotation
        # YAML/JSON arrays become immutable tuples; strings never become sequences.
        if get_origin(annotation) is tuple and isinstance(value, list):
            return tuple(value)
        literal = annotation
        if get_origin(annotation) in {Union, UnionType}:
            literal = next((item for item in get_args(annotation) if get_origin(item) is Literal), None)
            if value is None and type(None) in get_args(annotation):
                return value
        if get_origin(literal) is Literal:
            allowed = get_args(literal)
            if not any(type(value) is type(item) and value == item for item in allowed):
                raise ValueError("Unsupported literal value or type")
        return value


def validate_origin(value: str, *, allow_local: bool) -> str:
    """An origin has a scheme and authority, with no path/query/credentials."""
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Origin has an invalid port") from None
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.path or parts.query or parts.fragment or "*" in value
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError("Configure exact origins without paths, credentials or wildcards")
    if parts.scheme == "chrome-extension":
        if port is not None or not re.fullmatch(r"[a-p]{32}", parts.netloc):
            raise ValueError("Extension origin requires a 32-character extension ID")
    elif parts.scheme == "https":
        if parts.hostname in {"localhost", "127.0.0.1", "::1"} and not allow_local:
            raise ValueError("Production origins cannot be localhost")
    elif not (allow_local and parts.scheme == "http"
              and parts.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Use HTTPS, an extension origin, or local development HTTP")
    return value


class ModelServingSettings(ServingEntity):
    model_file: Path
    training_report: Path
    text_column: NonEmptyText
    sha256: Sha256
    preprocessing_sha256: Sha256
    result_schema_version: Literal[1]
    release_id: ReleaseId | None
    code_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None

    @model_validator(mode="after")
    def release_pair(self):
        if (self.release_id is None) != (self.code_revision is None):
            raise ValueError("Release ID and code revision must be assigned together")
        return self


class YouTubeSettings(ServingEntity):
    # Project provenance; the API key's actual project association is verified separately.
    project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")] | None = None
    live_enabled: bool
    analytics_use_approved: bool
    approval_reference: NonEmptyText | None
    api_key_secret_arn: Annotated[str, Field(
        pattern=r"^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$"
    )] | None
    project_quota_verified: bool
    visibility: Literal["public"]
    order: Literal["time"]
    text_format: Literal["plainText"]
    include_replies: Literal[False]
    default_max_comments: Annotated[int, Field(ge=1, le=1000)]
    maximum_comments: Annotated[int, Field(ge=1, le=1000)]
    page_size: Annotated[int, Field(ge=1, le=100)]
    connect_timeout_seconds: Annotated[int, Field(ge=1, le=10)]
    read_timeout_seconds: Annotated[int, Field(ge=1, le=10)]
    http_deadline_seconds: Annotated[int, Field(ge=1, le=30)]
    maximum_response_bytes: Annotated[int, Field(ge=1024, le=10485760)]
    maximum_http_attempts_per_call: Annotated[int, Field(ge=1, le=3)]
    global_http_attempts_per_second: PositiveInt
    project_daily_quota_units: PositiveInt
    working_quota_fraction: Annotated[float, Field(gt=0, le=1)]
    full_sample_reserved_units: PositiveInt
    quota_timezone: Literal["America/Los_Angeles"]

    @property
    def working_daily_quota_units(self) -> int:
        return int(Decimal(str(self.working_quota_fraction)) * self.project_daily_quota_units)

    @model_validator(mode="after")
    def validate_sample_and_approval(self):
        if max(self.connect_timeout_seconds, self.read_timeout_seconds) > self.http_deadline_seconds:
            raise ValueError("Provider connect/read timeouts must fit within its HTTP deadline")
        if self.default_max_comments > self.maximum_comments:
            raise ValueError("Default comment limit exceeds the maximum")
        required = (1 + math.ceil(self.maximum_comments / self.page_size)) * self.maximum_http_attempts_per_call
        if self.full_sample_reserved_units != required:
            raise ValueError("Full-sample reservation must cover metadata and every page's HTTP attempts")
        if required > self.working_daily_quota_units:
            raise ValueError("Working quota cannot reserve one maximum-size analysis")
        if self.approval_reference is not None and not self.approval_reference.strip():
            raise ValueError("Approval reference cannot contain only whitespace")
        if self.analytics_use_approved and not self.approval_reference:
            raise ValueError("Approved analytics use requires its recorded approval reference")
        if self.live_enabled and not (self.analytics_use_approved and self.approval_reference
                                      and self.api_key_secret_arn and self.project_quota_verified):
            raise ValueError("Live analysis requires approval, a secret reference and verified project quota")
        return self


class APISettings(ServingEntity):
    memory_mib: Annotated[int, Field(ge=128, le=10240)]
    timeout_seconds: Annotated[int, Field(ge=1, le=30)]
    reserved_concurrency: PositiveInt
    throttle_requests_per_second: PositiveInt
    throttle_burst: PositiveInt
    submission_requests_per_second: PositiveInt
    submission_burst: PositiveInt
    maximum_request_body_bytes: Annotated[int, Field(ge=1, le=2048)]
    allowed_origins: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def validate_route_limits(self):
        if (self.submission_requests_per_second > self.throttle_requests_per_second
                or self.submission_burst > self.throttle_burst):
            raise ValueError("Submission throttle must fit within the overall API throttle")
        if len(set(self.allowed_origins)) != len(self.allowed_origins):
            raise ValueError("Allowed origins must be unique")
        return self


class AdmissionSettings(ServingEntity):
    healthy_new_work_per_minute: PositiveInt
    maximum_new_work_per_minute: PositiveInt
    maximum_nonterminal_work: PositiveInt
    user_submissions_per_minute: PositiveInt
    user_reads_per_minute: PositiveInt
    user_maximum_active_references: PositiveInt

    @model_validator(mode="after")
    def validate_arrival_rate(self):
        if self.healthy_new_work_per_minute > self.maximum_new_work_per_minute:
            raise ValueError("Healthy arrival rate exceeds admission cap")
        return self


class WorkerSettings(ServingEntity):
    architecture: Literal["x86_64"]
    python_version: Literal["3.12"]
    memory_mib: Annotated[int, Field(ge=128, le=10240)]
    timeout_seconds: Annotated[int, Field(ge=1, le=900)]
    soft_deadline_seconds: PositiveInt
    lease_seconds: PositiveInt
    heartbeat_seconds: PositiveInt
    maximum_attempts: Annotated[int, Field(ge=1, le=20)]
    reserved_concurrency: PositiveInt
    sum_of_release_mapping_maximum_concurrency: Annotated[int, Field(ge=2)]
    batch_size: PositiveInt
    native_thread_limit: PositiveInt
    review_confidence_threshold: Probability
    restore_with_dvc: Literal[False]
    maximum_comment_characters: Annotated[int, Field(ge=1, le=5000)]
    maximum_result_bytes: Annotated[int, Field(ge=1, le=10485760)]

    @model_validator(mode="after")
    def validate_attempt_limits(self):
        if not self.heartbeat_seconds < self.soft_deadline_seconds < self.timeout_seconds < self.lease_seconds:
            raise ValueError("Require heartbeat < soft deadline < timeout < lease")
        if self.sum_of_release_mapping_maximum_concurrency > self.reserved_concurrency:
            raise ValueError("Release mapping concurrency exceeds worker reserved concurrency")
        return self


class QueueSettings(ServingEntity):
    type: Literal["standard"]
    per_serving_release: Literal[True]
    event_batch_size: Literal[1]
    event_batch_window_seconds: Literal[0]
    visibility_timeout_seconds: Annotated[int, Field(ge=1, le=43200)]
    maximum_receive_count: Annotated[int, Field(ge=5)]
    report_batch_item_failures: Literal[True]
    job_deadline_seconds: PositiveInt
    reconciler_interval_seconds: Annotated[int, Field(ge=60)]
    pending_outbox_retry_after_seconds: PositiveInt


class CacheSettings(ServingEntity):
    freshness_seconds: PositiveInt
    result_retention_seconds: Annotated[int, Field(ge=1, le=86400)]
    request_retention_seconds: Annotated[int, Field(ge=1, le=86400)]
    idempotency_retention_seconds: Annotated[int, Field(ge=1, le=86400)]
    download_url_seconds: Annotated[int, Field(ge=1, le=300)]
    s3_cleanup_backstop_days: Annotated[int, Field(ge=1, le=7)]
    force_refresh_enabled: Literal[False]

    @model_validator(mode="after")
    def validate_expiration(self):
        if not self.download_url_seconds <= self.freshness_seconds < self.result_retention_seconds:
            raise ValueError("Require download lifetime <= freshness < result retention")
        if self.idempotency_retention_seconds < self.request_retention_seconds:
            raise ValueError("Idempotency must outlive its owned request")
        return self


class StorageSettings(ServingEntity):
    bucket: Annotated[str, Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$")]
    serving_prefix: NonEmptyText
    table_name: Annotated[str, Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9_.-]+$")] | None
    public_access: Literal[False]
    encryption: Literal["AES256"]

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value):
        if ".." in value or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
            raise ValueError("Invalid S3 bucket name")
        return value


class AuthSettings(ServingEntity):
    cognito_issuer: NonEmptyText | None
    public_client_id: Annotated[str, Field(pattern=r"^[a-z0-9]{1,128}$")] | None
    client_secret: None
    authorization_flow: Literal["authorization_code"]
    pkce_method: Literal["S256"]
    scopes: tuple[str, ...]
    extension_id: Annotated[str, Field(pattern=r"^[a-p]{32}$")] | None

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value):
        if len(value) != 2 or set(value) != {"analysis/read", "analysis/write"}:
            raise ValueError("Configure exactly analysis/read and analysis/write scopes")
        return value


class ObservabilitySettings(ServingEntity):
    log_retention_days: Literal[14]
    include_comment_text_in_logs: Literal[False]
    include_tokens_in_logs: Literal[False]
    queue_age_alarm_seconds: PositiveInt
    dlq_alarm_message_count: Literal[1]


class ClientSettings(ServingEntity):
    poll_schedule_seconds: Annotated[tuple[PositiveInt, ...], Field(min_length=1)]
    poll_jitter: Literal[True]
    pause_polling_when_hidden: Literal[True]
    download_result_once: Literal[True]

    @field_validator("poll_schedule_seconds")
    @classmethod
    def validate_backoff(cls, value):
        if value != tuple(sorted(set(value))):
            raise ValueError("Poll delays must increase without repeated values")
        return value


class ServingSettings(ServingEntity):
    environment: Literal["development", "production"]
    region: Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z]+-\d+$")]
    target_active_users: PositiveInt
    monthly_budget_usd: Annotated[float, Field(gt=0)] | None
    model: ModelServingSettings
    youtube: YouTubeSettings
    api: APISettings
    admission: AdmissionSettings
    worker: WorkerSettings
    queue: QueueSettings
    cache: CacheSettings
    storage: StorageSettings
    auth: AuthSettings
    observability: ObservabilitySettings
    client: ClientSettings

    @model_validator(mode="after")
    def validate_system(self):
        if self.youtube.http_deadline_seconds > self.worker.heartbeat_seconds:
            raise ValueError("Provider HTTP deadline must fit between worker heartbeats")
        if self.queue.visibility_timeout_seconds < 6 * self.worker.timeout_seconds + self.queue.event_batch_window_seconds:
            raise ValueError("Queue visibility must be at least six worker timeouts plus its batch window")
        if self.queue.job_deadline_seconds <= self.worker.lease_seconds:
            raise ValueError("Job deadline must exceed a worker lease")
        if self.queue.job_deadline_seconds >= min(self.cache.request_retention_seconds, self.cache.result_retention_seconds):
            raise ValueError("Job deadline must fit inside request and result retention")
        if self.admission.maximum_nonterminal_work < self.worker.reserved_concurrency:
            raise ValueError("Work capacity must cover the configured workers")
        minimum_read_rate = math.ceil(60 / self.client.poll_schedule_seconds[-1]) * self.admission.user_maximum_active_references
        if minimum_read_rate > self.admission.user_reads_per_minute:
            raise ValueError("Steady polling would exceed the user's read allowance")
        prefix = self.storage.serving_prefix.split("/")
        if (prefix[:2] != ["serving", self.environment]
                or any(not re.fullmatch(r"[A-Za-z0-9_-]+", segment) for segment in prefix)):
            raise ValueError("Storage prefix must stay under serving/<environment> without traversal")
        for origin in self.api.allowed_origins:
            validate_origin(origin, allow_local=self.environment == "development")
        if self.auth.cognito_issuer is not None:
            if not re.fullmatch(rf"https://cognito-idp\.{re.escape(self.region)}\.amazonaws\.com/{re.escape(self.region)}_[A-Za-z0-9]+", self.auth.cognito_issuer):
                raise ValueError("Cognito issuer must identify a user pool in the configured region")
        if self.youtube.api_key_secret_arn and self.youtube.api_key_secret_arn.split(":")[3] != self.region:
            raise ValueError("YouTube secret reference must be in the configured region")
        if self.environment == "production":
            self.validate_for_deployment()
        return self

    def validate_for_deployment(self):
        """Check declared readiness only; external approval/resources need verification."""
        if self.environment != "production":
            raise ValueError("Deployment requires an explicit production configuration")
        requirements = {
            "monthly budget": self.monthly_budget_usd,
            "model release": self.model.release_id,
            "inference code revision": self.model.code_revision,
            "DynamoDB table": self.storage.table_name,
            "Cognito issuer": self.auth.cognito_issuer,
            "Cognito public client": self.auth.public_client_id,
            "extension ID": self.auth.extension_id,
            "allowed origins": self.api.allowed_origins,
            "YouTube secret reference": self.youtube.api_key_secret_arn,
            "YouTube quota verification": self.youtube.project_quota_verified,
            "analytics-use approval": self.youtube.analytics_use_approved and self.youtube.approval_reference,
        }
        missing = [name for name, value in requirements.items() if not value]
        if missing:
            raise ValueError("Production settings are incomplete: " + ", ".join(missing))
        if f"chrome-extension://{self.auth.extension_id}" not in self.api.allowed_origins:
            raise ValueError("Allow the configured extension's exact origin")
        return self
