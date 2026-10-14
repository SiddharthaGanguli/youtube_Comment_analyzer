"""Validate live readiness and read the API key from its configured AWS secret.

There is no fallback to a local file, environment key or DVC credential. AWS
authentication belongs to the caller's default role/profile credential chain.
"""

import json
import re

from src.Sentiment_analysis.entity.serving import YouTubeSettings


def validate_live_youtube(settings):
    """Revalidate copied settings before a runtime can contact AWS or Google."""
    try:
        youtube = YouTubeSettings.model_validate(settings.youtube.model_dump(warnings=False))
        if not youtube.live_enabled:
            raise ValueError("Disabled live provider")
        if youtube.api_key_secret_arn.split(":")[3] != settings.region:
            raise ValueError("Secret reference region differs from runtime")
        return youtube
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Live YouTube settings require approval, secret reference and verified quota") from None


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate secret JSON member")
        data[key] = value
    return data


def load_youtube_api_key(settings, secrets_client):
    """Accept one API key or exactly {\"api_key\": \"...\"}; never echo the value."""
    youtube = validate_live_youtube(settings)
    try:
        response = secrets_client.get_secret_value(SecretId=youtube.api_key_secret_arn)
    except Exception:
        raise ValueError("YouTube API key secret is unavailable") from None
    try:
        if not isinstance(response, dict) or "SecretBinary" in response:
            raise ValueError("Unsupported secret representation")
        raw = response["SecretString"]
        if not isinstance(raw, str) or not 1 <= len(raw.encode("utf-8")) <= 4096:
            raise ValueError("Invalid secret byte size")
        if raw.lstrip().startswith(("{", "[")):
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or set(data) != {"api_key"}:
                raise ValueError("Unsupported secret fields")
            key = data["api_key"]
        else:
            key = raw
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key):
            raise ValueError("Invalid API key")
        return key
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("YouTube API key secret must contain one valid API key") from None
