"""Bound request bytes before JSON validation and keep response errors safe."""

import json
import logging
from uuid import uuid4

from src.Sentiment_analysis.api.errors import error_response

logger = logging.getLogger(__name__)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated JSON property")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Nonfinite JSON constant")


class RequestBoundary:
    def __init__(self, app, *, maximum_bytes, clock):
        self.app = app
        self.maximum_bytes = maximum_bytes
        self.clock = clock

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scope.setdefault("state", {})["request_id"] = str(uuid4())
        started = False

        async def protected_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {b"cache-control", b"x-request-id"}]
                headers.extend([(b"cache-control", b"no-store"),
                                (b"x-request-id", scope["state"]["request_id"].encode())])
                message = dict(message, headers=headers)
            await send(message)

        async def fail(code):
            await error_response(code, scope, self.clock())(scope, receive, protected_send)

        if scope["method"] == "POST" and scope["path"] == "/v1/analyses":
            headers = scope.get("headers", [])
            sizes = [v for k, v in headers if k.lower() == b"content-length"]
            keys = [v for k, v in headers if k.lower() == b"idempotency-key"]
            encoding = [v for k, v in headers if k.lower() == b"content-encoding"]
            if len(sizes) > 1 or len(keys) > 1 or len(encoding) > 1 or (sizes and not sizes[0].isdigit()):
                return await fail("INVALID_INPUT")
            if encoding and encoding[0].lower() != b"identity":
                return await fail("INVALID_INPUT")
            if sizes and (len(sizes[0]) > 20 or int(sizes[0]) > self.maximum_bytes):
                return await fail("REQUEST_TOO_LARGE")
            chunks, total = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > self.maximum_bytes:
                    return await fail("REQUEST_TOO_LARGE")
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            if body:
                try:
                    json.loads(body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    return await fail("INVALID_INPUT")

            upstream_receive, replayed = receive, False

            async def replay_body():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await upstream_receive()

            receive = replay_body
        try:
            await self.app(scope, receive, protected_send)
        except Exception:
            # Don't log rejected values, SDK messages, tokens or signed URLs.
            logger.warning("Serving API dependency operation failed: request_id=%s", scope["state"]["request_id"])
            if started:
                raise RuntimeError("API response interrupted") from None
            await fail("DEPENDENCY_UNAVAILABLE")
