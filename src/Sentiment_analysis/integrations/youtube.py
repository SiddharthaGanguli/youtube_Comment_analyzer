"""One bounded attempt against the fixed YouTube Data API origin.

The pipeline spends quota and decides retries before calling this provider. The
transport never follows redirects, uses a proxy, or repeats a request itself.
"""

import http.client
import json
import math
import re
import ssl
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from src.Sentiment_analysis.entity.analysis import Video
from src.Sentiment_analysis.entity.worker import CommentPage, ProviderComment
from src.Sentiment_analysis.integrations.provider import ProviderError

_ORIGIN = "www.googleapis.com"
_VIDEO_PATH = "/youtube/v3/videos"
_COMMENTS_PATH = "/youtube/v3/commentThreads"
_VIDEO_FIELDS = "items(id,snippet(title,channelTitle,publishedAt),status(privacyStatus),statistics(commentCount))"
_COMMENT_FIELDS = ("nextPageToken,items(snippet(videoId,isPublic,topLevelComment(id,snippet("
                   "authorDisplayName,textDisplay,publishedAt,likeCount,parentId))))")
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"})
_CREDENTIAL_REASONS = frozenset({
    "keyInvalid", "accessNotConfigured", "ipRefererBlocked", "authError", "required",
    "API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
    "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_ANDROID_APP_BLOCKED", "API_KEY_IOS_APP_BLOCKED",
    "CREDENTIALS_MISSING", "SERVICE_DISABLED",
})
_TRANSIENT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})


def _https_transport(path, query, *, connect_timeout_seconds, read_timeout_seconds,
                     http_deadline_seconds, maximum_response_bytes):
    """Return (status, selected headers, bytes), using exactly one HTTPS request."""
    if path not in {_VIDEO_PATH, _COMMENTS_PATH}:
        raise ProviderError("INVALID_PROVIDER_RESPONSE")
    connection = None
    deadline = time.monotonic() + http_deadline_seconds

    def remaining(limit):
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ProviderError("PROVIDER_UNAVAILABLE", retryable=True)
        return min(limit, seconds)

    try:
        connection = http.client.HTTPSConnection(_ORIGIN, port=443,
                                                timeout=remaining(connect_timeout_seconds),
                                                context=ssl.create_default_context())
        connection.connect()
        # Keep this socket reference: http.client can detach connection.sock for
        # a Connection: close response while the body still needs to be read.
        active_socket = connection.sock
        parameters = {name: value for name, value in query.items() if name != "key"}
        active_socket.settimeout(remaining(read_timeout_seconds))
        connection.request("GET", path + "?" + urlencode(parameters), headers={
            "Accept": "application/json", "Accept-Encoding": "identity", "X-Goog-Api-Key": query["key"],
        })
        active_socket.settimeout(remaining(read_timeout_seconds))
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        length = response.getheader("Content-Length")
        if length is not None and (not re.fullmatch(r"[0-9]+", length)
                                   or len(length) > 10 or int(length) > maximum_response_bytes):
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        body = bytearray()
        while True:
            active_socket.settimeout(remaining(read_timeout_seconds))
            # read1 performs one buffered/socket read. read(n) may internally
            # wait for many chunks without returning to the deadline check.
            chunk = response.read1(min(65536, maximum_response_bytes + 1 - len(body)))
            remaining(read_timeout_seconds)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > maximum_response_bytes:
                raise ProviderError("INVALID_PROVIDER_RESPONSE")
        header = response.getheader("Retry-After")
        return response.status, ({"Retry-After": header} if header is not None else {}), bytes(body)
    except ProviderError:
        raise
    except Exception:
        # TLS/socket/HTTP diagnostics can contain the request target or key.
        raise ProviderError("PROVIDER_UNAVAILABLE", retryable=True) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _decode(body):
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_json_object,
                          parse_constant=_invalid_constant)
        if not isinstance(data, dict):
            raise ValueError("Response must be a JSON object")
        return data
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("INVALID_PROVIDER_RESPONSE") from None


def _retry_after(headers):
    value = next((value for key, value in headers.items()
                  if isinstance(key, str) and key.lower() == "retry-after"), None)
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        # The job deadline is less than one day. Longer delays therefore expire
        # the work without another call, rather than scheduling an early retry.
        return max(1, min(86400, int(value)))
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        seconds = math.ceil((stamp - datetime.now(timezone.utc)).total_seconds())
        return max(1, min(86400, seconds))
    except (ValueError, TypeError, OverflowError):
        return None


def _error(status, headers, body):
    reasons = set()
    try:
        error = _decode(body).get("error", {})
        if isinstance(error, dict):
            for entry in error.get("errors", ()):
                if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
                    reasons.add(entry["reason"])
            # Google also uses google.rpc.ErrorInfo in its modern error format.
            for entry in error.get("details", ()):
                if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
                    reasons.add(entry["reason"])
    except (ProviderError, TypeError):
        pass
    if reasons & _QUOTA_REASONS:
        return ProviderError("YOUTUBE_QUOTA_EXHAUSTED", circuit_reason="quota")
    if status == 401 or reasons & _CREDENTIAL_REASONS:
        return ProviderError("PROVIDER_CREDENTIALS_INVALID", circuit_reason="credentials")
    if status == 429 or 500 <= status <= 599 or reasons & _TRANSIENT_REASONS:
        return ProviderError("PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=_retry_after(headers))
    if "commentsDisabled" in reasons:
        return ProviderError("COMMENTS_DISABLED")
    if status == 404 or "videoNotFound" in reasons:
        return ProviderError("VIDEO_NOT_FOUND")
    if status == 403:
        return ProviderError("VIDEO_NOT_PUBLIC")
    return ProviderError("INVALID_PROVIDER_RESPONSE")


def _video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise ProviderError("INVALID_PROVIDER_RESPONSE")
    return value


def _page_token(value):
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 2048
                              or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ProviderError("INVALID_PROVIDER_RESPONSE")
    return value


class YouTubeCommentProvider:
    is_fixture = False

    def __init__(self, settings, api_key, *, transport=None):
        if not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", api_key):
            raise ValueError("YouTube provider requires a valid API key")
        if transport is not None:
            if settings.environment != "development" or not callable(transport):
                raise ValueError("Injected YouTube transports are allowed only in development")
        else:
            from src.Sentiment_analysis.config.youtube import validate_live_youtube
            validate_live_youtube(settings)
        self.settings, self._api_key = settings, api_key
        self._transport = transport or _https_transport

    def _request(self, path, query):
        options = self.settings.youtube
        try:
            response = self._transport(path, {**query, "key": self._api_key, "prettyPrint": "false"},
                                       connect_timeout_seconds=options.connect_timeout_seconds,
                                       read_timeout_seconds=options.read_timeout_seconds,
                                       http_deadline_seconds=options.http_deadline_seconds,
                                       maximum_response_bytes=options.maximum_response_bytes)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("PROVIDER_UNAVAILABLE", retryable=True) from None
        if (not isinstance(response, tuple) or len(response) != 3 or type(response[0]) is not int
                or not 100 <= response[0] <= 599 or not isinstance(response[1], Mapping)
                or not isinstance(response[2], bytes) or len(response[2]) > options.maximum_response_bytes):
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        status, headers, body = response
        if status != 200:
            raise _error(status, headers, body)
        data = _decode(body)
        if "error" in data or not isinstance(data.get("items"), list):
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        return data

    def video(self, video_id):
        video_id = _video_id(video_id)
        data = self._request(_VIDEO_PATH, {"id": video_id, "part": "snippet,status,statistics",
                                          "fields": _VIDEO_FIELDS})
        if not data["items"]:
            raise ProviderError("VIDEO_NOT_FOUND")
        try:
            if len(data["items"]) != 1 or data["items"][0]["id"] != video_id:
                raise ValueError("Unexpected video identity")
            item = data["items"][0]
            privacy = item["status"]["privacyStatus"]
            if privacy not in {"public", "private", "unlisted"}:
                raise ValueError("Invalid video privacy value")
            if privacy != "public":
                raise ProviderError("VIDEO_NOT_PUBLIC")
            snippet = item["snippet"]
            statistics = item.get("statistics", {})
            if not isinstance(statistics, dict):
                raise ValueError("Invalid video statistics")
            count = statistics.get("commentCount")
            if "commentCount" in statistics:
                if not isinstance(count, str) or not re.fullmatch(r"[0-9]{1,20}", count):
                    raise ValueError("Invalid comment count")
                count = int(count)
                if count > 18446744073709551615:
                    raise ValueError("Invalid unsigned comment count")
            return Video(id=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
                         title=snippet["title"], channel_title=snippet["channelTitle"],
                         published_at=snippet["publishedAt"], total_comment_count=count)
        except ProviderError:
            raise
        except (ValueError, TypeError, KeyError, IndexError):
            raise ProviderError("INVALID_PROVIDER_RESPONSE") from None

    def comments(self, video_id, page_token, max_results):
        video_id, page_token = _video_id(video_id), _page_token(page_token)
        if type(max_results) is not int or not 1 <= max_results <= min(100, self.settings.youtube.page_size):
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        query = {"videoId": video_id, "part": "snippet", "maxResults": str(max_results),
                 "order": "time", "textFormat": "plainText", "fields": _COMMENT_FIELDS}
        if page_token is not None:
            query["pageToken"] = page_token
        data = self._request(_COMMENTS_PATH, query)
        try:
            if len(data["items"]) > max_results:
                raise ValueError("Provider returned too many comment threads")
            rows = []
            for item in data["items"]:
                thread = item["snippet"]
                if thread["videoId"] != video_id or type(thread["isPublic"]) is not bool:
                    raise ValueError("Invalid thread identity or visibility")
                if not thread["isPublic"]:
                    raise ProviderError("VIDEO_NOT_PUBLIC")
                comment = thread["topLevelComment"]
                snippet = comment["snippet"]
                if "parentId" in snippet or "videoId" in snippet and snippet["videoId"] != video_id:
                    raise ValueError("Provider returned a reply or another video's comment")
                identifier = comment["id"]
                if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in identifier)):
                    raise ValueError("Invalid comment identity")
                if not isinstance(snippet["textDisplay"], str):
                    raise ValueError("Provider must return plain text")
                rows.append(ProviderComment(id=identifier, author=snippet.get("authorDisplayName"),
                                            published_at=snippet["publishedAt"], likes=snippet["likeCount"],
                                            text=snippet["textDisplay"]))
            return CommentPage(comments=tuple(rows), next_page_token=_page_token(data.get("nextPageToken")))
        except ProviderError:
            raise
        except (ValueError, TypeError, KeyError, IndexError):
            raise ProviderError("INVALID_PROVIDER_RESPONSE") from None
