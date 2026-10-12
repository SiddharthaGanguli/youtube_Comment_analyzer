"""Small HTTP/runtime entities; inference and credential values do not belong here."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from src.Sentiment_analysis.entity.analysis import ModelDetails
from src.Sentiment_analysis.entity.serving import ServingEntity


class Principal(ServingEntity):
    owner: Annotated[str, Field(min_length=1, max_length=256, repr=False)]
    scopes: tuple[str, ...]


class Liveness(ServingEntity):
    status: Literal["ok"] = "ok"


def release_fingerprint(model, code_revision: str) -> str:
    binding = {"model_sha256": model.sha256, "preprocessing_sha256": model.preprocessing_sha256,
               "code_revision": code_revision, "review_confidence_threshold": model.review_confidence_threshold,
               "result_schema_version": model.result_schema_version}
    data = json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sr_" + hashlib.sha256(data).hexdigest()


class ReleaseManifest(ServingEntity):
    schema_version: Literal[1] = 1
    code_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    details: ModelDetails

    @model_validator(mode="after")
    def verify_release_binding(self):
        if self.details.model.release_id != release_fingerprint(self.details.model, self.code_revision):
            raise ValueError("Release ID must bind model, preprocessing, code, threshold and schema")
        return self
