"""Public auth provisioning settings. No secret or access token belongs here."""

from typing import Annotated, Literal

from pydantic import Field

from src.Sentiment_analysis.entity.serving import ServingEntity


class AuthInfrastructureSettings(ServingEntity):
    environment: Literal["development", "production"]
    region: Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z]+-\d+$")]
    cognito_domain_prefix: Annotated[str, Field(min_length=1, max_length=45, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    access_token_minutes: Annotated[int, Field(ge=5, le=60)]
    refresh_token_days: Annotated[int, Field(ge=1, le=30)]
    callback_path: Literal["cognito"]
    logout_path: Literal["logout"]
    self_signup_enabled: bool
