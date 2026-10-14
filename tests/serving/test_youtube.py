"""Recorded Google response shapes; these tests never contact YouTube."""

import http.client
import json
import os
import ssl
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.entity.analysis import Video
from src.Sentiment_analysis.entity.worker import CommentPage
from src.Sentiment_analysis.integrations.fixture_comments import ProviderError
from src.Sentiment_analysis.integrations.youtube import YouTubeCommentProvider, _https_transport
from tests.serving.fixtures import TIME, VIDEO_ID

KEY = "synthetic-youtube-key-for-offline-tests"


def video_response():
    return {"items": [{"id": VIDEO_ID,
                       "snippet": {"title": "A fixture video", "channelTitle": "Fixture channel",
                                   "publishedAt": TIME},
                       "status": {"privacyStatus": "public"},
                       "statistics": {"commentCount": "42"}}]}


def thread(identifier="top-level-1"):
    return {"id": "thread-" + identifier,
            "snippet": {"videoId": VIDEO_ID, "isPublic": True,
                        "topLevelComment": {"id": identifier,
                                            "snippet": {"videoId": VIDEO_ID,
                                                        "authorDisplayName": "Fixture author",
                                                        "textDisplay": "Useful & clear 😊",
                                                        "textOriginal": "Owner-only original text",
                                                        "publishedAt": TIME, "likeCount": 3}}},
            "replies": {"comments": [{"id": "reply-must-not-be-analyzed",
                                       "snippet": {"textDisplay": "A reply"}}]}}


def page_response(*identifiers, token=None):
    data = {"items": [thread(identifier) for identifier in identifiers]}
    if token is not None:
        data["nextPageToken"] = token
    return data


class RecordedTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, path, query, **limits):
        self.calls.append((path, dict(query), dict(limits)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, {"content-type": "application/json"}, json.dumps(response, ensure_ascii=False).encode("utf-8")


class YouTubeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = ServingConfigurationManager().get_serving_config()

    def provider(self, *responses):
        transport = RecordedTransport(*responses)
        return YouTubeCommentProvider(self.settings, KEY, transport=transport), transport

    def error(self, action, code, *, retryable=False, circuit_reason=None):
        with self.assertRaises(ProviderError) as caught:
            action()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.retryable, retryable)
        self.assertEqual(error.circuit_reason, circuit_reason)
        self.assertNotIn(KEY, str(error))
        self.assertNotIn(KEY, repr(error))
        return error

    def test_metadata_maps_public_video_and_fixed_request(self):
        provider, transport = self.provider(video_response())
        video = provider.video(VIDEO_ID)
        self.assertIsInstance(video, Video)
        self.assertEqual(video.id, VIDEO_ID)
        self.assertEqual(video.url, f"https://www.youtube.com/watch?v={VIDEO_ID}")
        self.assertEqual(video.title, "A fixture video")
        self.assertEqual(video.channel_title, "Fixture channel")
        self.assertEqual(video.published_at.tzinfo, timezone.utc)
        self.assertEqual(video.total_comment_count, 42)
        path, query, limits = transport.calls[0]
        self.assertEqual(path, "/youtube/v3/videos")
        self.assertEqual(query["id"], VIDEO_ID)
        self.assertEqual(set(query["part"].split(",")), {"snippet", "statistics", "status"})
        self.assertEqual(query["key"], KEY)
        self.assertEqual(limits, {"connect_timeout_seconds": self.settings.youtube.connect_timeout_seconds,
                                  "read_timeout_seconds": self.settings.youtube.read_timeout_seconds,
                                  "http_deadline_seconds": self.settings.youtube.http_deadline_seconds,
                                  "maximum_response_bytes": self.settings.youtube.maximum_response_bytes})
        self.assertEqual(len(transport.calls), 1)

    def test_unavailable_total_count_is_unknown_instead_of_zero(self):
        for statistics in ({}, None):
            response = video_response()
            if statistics is None:
                del response["items"][0]["statistics"]
            else:
                response["items"][0]["statistics"] = statistics
            with self.subTest(statistics=statistics):
                provider, _ = self.provider(response)
                self.assertIsNone(provider.video(VIDEO_ID).total_comment_count)

    def test_missing_private_and_unlisted_videos_are_not_analyzed(self):
        provider, transport = self.provider({"items": []})
        self.error(lambda: provider.video(VIDEO_ID), "VIDEO_NOT_FOUND")
        self.assertEqual(len(transport.calls), 1)
        for visibility in ("private", "unlisted"):
            response = video_response()
            response["items"][0]["status"]["privacyStatus"] = visibility
            with self.subTest(visibility=visibility):
                provider, _ = self.provider(response)
                self.error(lambda: provider.video(VIDEO_ID), "VIDEO_NOT_PUBLIC")

    def test_malformed_metadata_never_becomes_trusted_video(self):
        responses = [{}, {"items": None}, {"items": {}}, {"items": [None]},
                     {"items": video_response()["items"] * 2}]
        for path, value in (("id", "aaaaaaaaaaa"), ("snippet", []), ("status", None),
                            ("statistics", [])):
            response = video_response()
            response["items"][0][path] = value
            responses.append(response)
        for value in (None, True, -1, "-1", "1.5", "NaN", "18446744073709551616", "9" * 5000):
            response = video_response()
            response["items"][0]["statistics"]["commentCount"] = value
            responses.append(response)
        for field, value in (("title", None), ("channelTitle", 1), ("publishedAt", "not-a-date"),
                             ("publishedAt", "2026-10-12T10:00:00"), ("publishedAt", 42)):
            response = video_response()
            response["items"][0]["snippet"][field] = value
            responses.append(response)
        for index, response in enumerate(responses):
            with self.subTest(case=index):
                provider, _ = self.provider(response)
                self.error(lambda: provider.video(VIDEO_ID), "INVALID_PROVIDER_RESPONSE")

    def test_top_level_plain_text_mapping_ignores_replies_and_owner_only_text(self):
        provider, transport = self.provider(page_response("top-1", "top-2", token="next-token_123"))
        page = provider.comments(VIDEO_ID, None, 2)
        self.assertIsInstance(page, CommentPage)
        self.assertEqual([row.id for row in page.comments], ["top-1", "top-2"])
        self.assertEqual(page.comments[0].text, "Useful & clear 😊")
        self.assertEqual(page.comments[0].author, "Fixture author")
        self.assertEqual(page.comments[0].likes, 3)
        self.assertEqual(page.comments[0].published_at.tzinfo, timezone.utc)
        self.assertEqual(page.next_page_token, "next-token_123")
        path, query, _ = transport.calls[0]
        self.assertEqual(path, "/youtube/v3/commentThreads")
        self.assertEqual(query["videoId"], VIDEO_ID)
        self.assertEqual(query["part"], "snippet")
        self.assertEqual(query["textFormat"], "plainText")
        self.assertEqual(query["fields"],
                         "nextPageToken,items(snippet(videoId,isPublic,topLevelComment(id,snippet("
                         "authorDisplayName,textDisplay,publishedAt,likeCount,parentId))))")
        self.assertEqual(query["order"], "time")
        self.assertEqual(query["maxResults"], "2")
        self.assertNotIn("pageToken", query)
        self.assertNotIn("moderationStatus", query)
        self.assertNotIn("searchTerms", query)
        self.assertEqual(len(transport.calls), 1)

    def test_page_token_and_remaining_sample_are_forwarded_without_auto_pagination(self):
        provider, transport = self.provider(page_response("last", token="more"))
        page = provider.comments(VIDEO_ID, "prior-token_123", 1)
        self.assertEqual(len(page.comments), 1)
        self.assertEqual(page.next_page_token, "more")
        self.assertEqual(transport.calls[0][1]["pageToken"], "prior-token_123")
        self.assertEqual(transport.calls[0][1]["maxResults"], "1")
        self.assertEqual(len(transport.calls), 1)

    def test_duplicate_ids_remain_for_processor_accounting(self):
        provider, _ = self.provider(page_response("repeated", "repeated"))
        page = provider.comments(VIDEO_ID, None, 2)
        self.assertEqual([row.id for row in page.comments], ["repeated", "repeated"])

    def test_empty_page_has_no_invented_comments(self):
        provider, _ = self.provider(page_response())
        page = provider.comments(VIDEO_ID, None, 10)
        self.assertEqual(page.comments, ())
        self.assertIsNone(page.next_page_token)

    def test_author_can_be_unavailable_and_plain_text_is_kept_exactly(self):
        response = page_response("one")
        snippet = response["items"][0]["snippet"]["topLevelComment"]["snippet"]
        del snippet["authorDisplayName"]
        snippet["textDisplay"] = "<b>literal user text</b> &amp; \n"
        provider, _ = self.provider(response)
        row = provider.comments(VIDEO_ID, None, 1).comments[0]
        self.assertIsNone(row.author)
        self.assertEqual(row.text, snippet["textDisplay"])

    def test_foreign_nonpublic_and_reply_rows_cannot_enter_public_top_level_sample(self):
        for field, value in (("videoId", "aaaaaaaaaaa"), ("isPublic", False), ("isPublic", 1)):
            response = page_response("one")
            response["items"][0]["snippet"][field] = value
            with self.subTest(field=field, value=value):
                provider, _ = self.provider(response)
                self.error(lambda: provider.comments(VIDEO_ID, None, 1),
                           "VIDEO_NOT_PUBLIC" if field == "isPublic" and value is False else "INVALID_PROVIDER_RESPONSE")
        for field, value in (("videoId", "aaaaaaaaaaa"), ("parentId", "parent-comment")):
            response = page_response("one")
            response["items"][0]["snippet"]["topLevelComment"]["snippet"][field] = value
            with self.subTest(field=field):
                provider, _ = self.provider(response)
                self.error(lambda: provider.comments(VIDEO_ID, None, 1), "INVALID_PROVIDER_RESPONSE")

    def test_overfilled_page_is_rejected_without_silent_truncation(self):
        provider, _ = self.provider(page_response("one", "two"))
        self.error(lambda: provider.comments(VIDEO_ID, None, 1), "INVALID_PROVIDER_RESPONSE")

    def test_malformed_page_rows_are_rejected(self):
        responses = [{}, {"items": None}, {"items": {}}, {"items": [None]},
                     {"items": [{"snippet": {}}]}]
        for field, value in (("id", ""), ("id", " "), ("id", "one\n"), ("id", "one\x7f"),
                             ("id", "x" * 257), ("id", 1),
                             ("likeCount", -1), ("likeCount", True), ("likeCount", "2"),
                             ("publishedAt", "not-a-date"), ("publishedAt", "2026-10-12T10:00:00"),
                             ("textDisplay", []), ("authorDisplayName", 42)):
            response = page_response("one")
            top = response["items"][0]["snippet"]["topLevelComment"]
            if field == "id":
                top[field] = value
            else:
                top["snippet"][field] = value
            responses.append(response)
        for token in ("", "x" * 2049, 42, []):
            response = page_response("one")
            response["nextPageToken"] = token
            responses.append(response)
        for field in ("textDisplay", "publishedAt", "likeCount"):
            response = page_response("one")
            del response["items"][0]["snippet"]["topLevelComment"]["snippet"][field]
            responses.append(response)
        for index, response in enumerate(responses):
            with self.subTest(case=index):
                provider, _ = self.provider(response)
                self.error(lambda: provider.comments(VIDEO_ID, None, 1), "INVALID_PROVIDER_RESPONSE")

    def test_invalid_video_ids_tokens_and_page_sizes_never_reach_transport(self):
        for video_id in (None, "short", "a" * 12, "https://evil.example/", "aaaaaaaaaa\n", 42):
            with self.subTest(video_id=video_id):
                provider, transport = self.provider()
                self.error(lambda: provider.video(video_id), "INVALID_PROVIDER_RESPONSE")
                self.assertEqual(transport.calls, [])
        for token in ("", "x" * 2049, "line\nbreak", "token with space", 42, []):
            with self.subTest(token=token):
                provider, transport = self.provider()
                self.error(lambda: provider.comments(VIDEO_ID, token, 1), "INVALID_PROVIDER_RESPONSE")
                self.assertEqual(transport.calls, [])
        for maximum in (0, 101, -1, True, 1.5, "1"):
            with self.subTest(maximum=maximum):
                provider, transport = self.provider()
                self.error(lambda: provider.comments(VIDEO_ID, None, maximum), "INVALID_PROVIDER_RESPONSE")
                self.assertEqual(transport.calls, [])

    def test_error_reasons_distinguish_video_quota_credentials_and_transient_failures(self):
        cases = [(403, "commentsDisabled", "COMMENTS_DISABLED", False, None),
                 (404, "videoNotFound", "VIDEO_NOT_FOUND", False, None),
                 (403, "quotaExceeded", "YOUTUBE_QUOTA_EXHAUSTED", False, "quota"),
                 (403, "dailyLimitExceeded", "YOUTUBE_QUOTA_EXHAUSTED", False, "quota"),
                 (400, "keyInvalid", "PROVIDER_CREDENTIALS_INVALID", False, "credentials"),
                 (401, "unknown", "PROVIDER_CREDENTIALS_INVALID", False, "credentials"),
                 (403, "accessNotConfigured", "PROVIDER_CREDENTIALS_INVALID", False, "credentials"),
                 (403, "forbidden", "VIDEO_NOT_PUBLIC", False, None),
                 (429, "rateLimitExceeded", "PROVIDER_UNAVAILABLE", True, None),
                 (403, "userRateLimitExceeded", "PROVIDER_UNAVAILABLE", True, None),
                 (500, "backendError", "PROVIDER_UNAVAILABLE", True, None),
                 (503, "backendError", "PROVIDER_UNAVAILABLE", True, None)]
        for status, reason, code, retryable, circuit in cases:
            body = {"error": {"code": status, "message": "private upstream " + KEY,
                              "errors": [{"reason": reason, "message": "private " + KEY}]}}
            with self.subTest(status=status, reason=reason):
                provider, transport = self.provider((status, {}, json.dumps(body).encode()))
                self.error(lambda: provider.comments(VIDEO_ID, None, 1), code,
                           retryable=retryable, circuit_reason=circuit)
                self.assertEqual(len(transport.calls), 1)

    def test_modern_google_credential_error_is_safe_and_opens_credential_circuit(self):
        response = {"error": {"message": KEY, "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                                         "reason": "API_KEY_SERVICE_BLOCKED"}]}}
        provider, _ = self.provider((403, {}, json.dumps(response).encode()))
        self.error(lambda: provider.video(VIDEO_ID), "PROVIDER_CREDENTIALS_INVALID", circuit_reason="credentials")

    def test_retry_after_preserves_upstream_delay_and_does_not_trigger_adapter_retries(self):
        for supplied, expected in (("7", 7), ("120", 120), ("0", 1), ("999999", 86400),
                                   ("9" * 128, 86400), ("9" * 129, None), ("-2", None), ("bad", None)):
            provider, transport = self.provider((429, {"Retry-After": supplied}, b'{}'))
            error = self.error(lambda: provider.video(VIDEO_ID), "PROVIDER_UNAVAILABLE", retryable=True)
            self.assertEqual(error.retry_after_seconds, expected)
            self.assertEqual(len(transport.calls), 1)

    def test_retry_after_http_date_preserves_delay_and_bounds_expired_or_distant_dates(self):
        now = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        for seconds, expected in ((120, 120), (-120, 1), (172800, 86400)):
            header = format_datetime(now + timedelta(seconds=seconds), usegmt=True)
            with self.subTest(seconds=seconds), \
                    patch("src.Sentiment_analysis.integrations.youtube.datetime") as clock:
                clock.now.return_value = now
                provider, transport = self.provider((429, {"Retry-After": header}, b'{}'))
                error = self.error(lambda: provider.video(VIDEO_ID), "PROVIDER_UNAVAILABLE", retryable=True)
                self.assertEqual(error.retry_after_seconds, expected)
                self.assertEqual(len(transport.calls), 1)

    def test_provider_error_rejects_invalid_retry_delays(self):
        for seconds in (0, -1, 86401, True, 1.5, "120"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                ProviderError("PROVIDER_UNAVAILABLE", retryable=True, retry_after_seconds=seconds)

    def test_non_json_duplicate_keys_nonfinite_and_oversized_json_are_rejected(self):
        bodies = [b'<html>Gateway failure</html>', b'[]', b'null', b'{"items":[],"items":[]}',
                  b'{"items":[],"x":NaN}', b'\xff',
                  b' ' * (self.settings.youtube.maximum_response_bytes + 1)]
        for body in bodies:
            with self.subTest(body_length=len(body)):
                provider, transport = self.provider((200, {}, body))
                self.error(lambda: provider.video(VIDEO_ID), "INVALID_PROVIDER_RESPONSE")
                self.assertEqual(len(transport.calls), 1)

    def test_invalid_transport_response_envelopes_are_rejected(self):
        for response in ((True, {}, b'{}'), (99, {}, b'{}'), (200, [], b'{}'),
                         (200, {}, "not-bytes"), (200, {}),
                         (200, {}, b'{"items":[],"error":{"message":"upstream error"}}')):
            with self.subTest(response=response):
                provider, transport = self.provider(response)
                self.error(lambda: provider.video(VIDEO_ID), "INVALID_PROVIDER_RESPONSE")
                self.assertEqual(len(transport.calls), 1)

    def test_redirect_and_transport_failures_are_one_attempt_and_safe(self):
        for status in (301, 302, 307, 308):
            provider, transport = self.provider((status, {"Location": "https://evil.example/" + KEY}, b""))
            self.error(lambda: provider.video(VIDEO_ID), "INVALID_PROVIDER_RESPONSE")
            self.assertEqual(len(transport.calls), 1)
        for error in (TimeoutError(KEY), OSError(KEY), http.client.HTTPException(KEY)):
            provider, transport = self.provider(error)
            self.error(lambda: provider.video(VIDEO_ID), "PROVIDER_UNAVAILABLE", retryable=True)
            self.assertEqual(len(transport.calls), 1)

    def test_provider_repr_does_not_expose_api_key(self):
        provider, _ = self.provider(video_response())
        self.assertNotIn(KEY, repr(provider))
        self.assertNotIn(KEY, str(provider))
        self.assertNotIn(KEY, repr(provider.video(VIDEO_ID)))

    def test_invalid_key_rejection_does_not_echo_secret_value(self):
        for key in (KEY + "\n", KEY * 20, ""):
            with self.subTest(key_length=len(key)):
                with self.assertRaises(ValueError) as caught:
                    YouTubeCommentProvider(self.settings, key, transport=RecordedTransport())
                self.assertNotIn(KEY, str(caught.exception))

    def test_default_transport_is_gated_and_injected_transport_is_development_only(self):
        with patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection") as connection:
            with self.assertRaises((ValueError, ProviderError)):
                YouTubeCommentProvider(self.settings, KEY)
            self.assertFalse(connection.called)
        production = self.settings.model_copy(update={"environment": "production"})
        with self.assertRaises(ValueError):
            YouTubeCommentProvider(production, KEY, transport=RecordedTransport())

    def test_live_flag_alone_cannot_bypass_individual_readiness_gates(self):
        ready = {"live_enabled": True, "analytics_use_approved": True,
                 "approval_reference": "synthetic-recorded-approval",
                 "api_key_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-key",
                 "project_quota_verified": True}
        for field in ready:
            values = dict(ready)
            values[field] = False if isinstance(values[field], bool) else None
            settings = self.settings.model_copy(update={"youtube": self.settings.youtube.model_copy(update=values)})
            with self.subTest(missing=field), \
                    patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection") as connection:
                with self.assertRaises(ValueError):
                    YouTubeCommentProvider(settings, KEY)
                connection.assert_not_called()


class YouTubeHTTPSBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.settings = ServingConfigurationManager().get_serving_config().youtube
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.getheaders.return_value = [("Content-Type", "application/json")]
        self.response.getheader.side_effect = lambda name, default=None: default
        self.response.read1.side_effect = [b'{"items":[]}', b""]

    def invoke(self):
        return _https_transport("/youtube/v3/videos", {"id": VIDEO_ID, "key": KEY},
                               connect_timeout_seconds=self.settings.connect_timeout_seconds,
                               read_timeout_seconds=self.settings.read_timeout_seconds,
                               http_deadline_seconds=self.settings.http_deadline_seconds,
                               maximum_response_bytes=self.settings.maximum_response_bytes)

    def test_fixed_tls_host_ignores_ambient_proxies_and_closes_connection(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://evil.example:3128", "ALL_PROXY": "http://evil.example:3128"}), \
                patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                      return_value=self.connection) as constructor:
            status, _, body = self.invoke()
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"items":[]}')
        self.assertEqual(constructor.call_args.args[0], "www.googleapis.com")
        self.assertEqual(constructor.call_args.kwargs["timeout"], self.settings.connect_timeout_seconds)
        context = constructor.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(self.connection.connect.call_count, 1)
        self.assertEqual(self.connection.request.call_count, 1)
        args = self.connection.request.call_args.args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].startswith("/youtube/v3/videos?"))
        self.assertNotIn(KEY, args[1])
        self.assertNotIn("key=", args[1])
        self.assertNotIn("evil.example", args[1])
        self.assertEqual(self.connection.request.call_args.kwargs["headers"]["X-Goog-Api-Key"], KEY)
        self.assertEqual(self.connection.request.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
        self.connection.close.assert_called_once()

    def test_tls_transport_does_not_follow_redirect(self):
        self.response.status = 302
        self.response.getheaders.return_value = [("Location", "https://evil.example/" + KEY)]
        with patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                   return_value=self.connection) as constructor:
            status, _, _ = self.invoke()
        self.assertEqual(status, 302)
        self.assertEqual(constructor.call_count, 1)
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once()

    def test_stream_overflow_and_timeout_always_close_without_retry(self):
        for failure in (TimeoutError(KEY), b"x" * (self.settings.maximum_response_bytes + 1)):
            connection = MagicMock()
            response = connection.getresponse.return_value
            response.status = 200
            response.getheaders.return_value = []
            response.getheader.side_effect = lambda name, default=None: default
            response.read1.side_effect = (failure if isinstance(failure, Exception) else
                                         [failure[index:index + 65536] for index in range(0, len(failure), 65536)])
            with self.subTest(failure_type=type(failure).__name__), \
                    patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                          return_value=connection) as constructor:
                with self.assertRaises((ProviderError, OSError, ValueError)) as caught:
                    self.invoke()
                if isinstance(caught.exception, ProviderError):
                    self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(constructor.call_count, 1)
                connection.request.assert_called_once()
                connection.close.assert_called_once()

    def test_connect_timeout_closes_without_request_or_retry(self):
        self.connection.connect.side_effect = TimeoutError(KEY)
        with patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                   return_value=self.connection) as constructor:
            with self.assertRaises(ProviderError) as caught:
                self.invoke()
        self.assertEqual(caught.exception.code, "PROVIDER_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(constructor.call_count, 1)
        self.connection.request.assert_not_called()
        self.connection.close.assert_called_once()

    def test_declared_oversize_or_compressed_body_is_rejected_before_reading(self):
        headers = ({"Content-Length": str(self.settings.maximum_response_bytes + 1)},
                   {"Content-Length": "-1"}, {"Content-Length": "9" * 5000},
                   {"Content-Encoding": "gzip"})
        for values in headers:
            with self.subTest(header=list(values)):
                self.response.getheader.side_effect = lambda name, default=None: values.get(name, default)
                with patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                           return_value=self.connection):
                    with self.assertRaises(ProviderError) as caught:
                        self.invoke()
                self.assertEqual(caught.exception.code, "INVALID_PROVIDER_RESPONSE")
                self.response.read1.assert_not_called()
                self.connection.close.assert_called()

    def test_total_deadline_stops_a_slow_stream_after_one_read(self):
        with patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                   return_value=self.connection), \
                patch("src.Sentiment_analysis.integrations.youtube.time.monotonic",
                      side_effect=[0, 0, 0, 0, 0, self.settings.http_deadline_seconds + 1]):
            with self.assertRaises(ProviderError) as caught:
                self.invoke()
        self.assertEqual(caught.exception.code, "PROVIDER_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)
        self.response.read1.assert_called_once()
        self.connection.close.assert_called_once()

    def test_detached_connection_socket_still_receives_read_deadline(self):
        active_socket = self.connection.sock
        def response_with_detached_socket():
            self.connection.sock = None
            return self.response
        self.connection.getresponse.side_effect = response_with_detached_socket
        with patch("src.Sentiment_analysis.integrations.youtube.http.client.HTTPSConnection",
                   return_value=self.connection):
            status, _, _ = self.invoke()
        self.assertEqual(status, 200)
        self.assertGreaterEqual(active_socket.settimeout.call_count, 4)
        self.connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
