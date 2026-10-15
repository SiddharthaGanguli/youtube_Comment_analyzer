"""Apply non-secret deployment identifiers to the packaged serving settings.

CloudFormation supplies resource names after creation. Credentials continue to
come from the Lambda role; this module never accepts keys or credential files.
"""

import json
import os

from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.entity.serving import ServingSettings


OVERRIDE_FIELDS = {
    "model": {"release_id", "code_revision"},
    "storage": {"bucket", "serving_prefix", "table_name"},
    "auth": {"cognito_issuer", "public_client_id", "extension_id"},
    "api": {"allowed_origins", "reserved_concurrency"},
    "worker": {"reserved_concurrency", "sum_of_release_mapping_maximum_concurrency"},
    "youtube": {"live_enabled", "analytics_use_approved", "approval_reference"},
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate deployment setting")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite deployment value")


def runtime_settings(environ=None, *, manager=ServingConfigurationManager):
    """Load validated YAML, then a small explicit resource-identifier override."""
    environ = os.environ if environ is None else environ
    settings = manager(environ.get("SERVING_CONFIG_PATH")).get_serving_config()
    raw = environ.get("SERVING_RUNTIME_OVERRIDES_JSON")
    if raw is None:
        return settings
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16384:
            raise ValueError
        overrides = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(overrides, dict) or set(overrides) - OVERRIDE_FIELDS.keys():
            raise ValueError
        data = settings.model_dump()
        for section, values in overrides.items():
            if not isinstance(values, dict) or set(values) - OVERRIDE_FIELDS[section]:
                raise ValueError
            data[section] = {**data[section], **values}
        return ServingSettings.model_validate(data)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Deployment overrides are invalid; use only supported non-secret settings") from None
