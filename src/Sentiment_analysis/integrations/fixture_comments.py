"""Explicit development provider. It reads synthetic data and makes no HTTP calls."""

import json
from pathlib import Path

from src.Sentiment_analysis.entity.analysis import Video
from src.Sentiment_analysis.entity.worker import CommentPage


class ProviderError(Exception):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class FixtureCommentProvider:
    is_fixture = True

    def __init__(self, settings, data):
        if settings.environment != "development":
            raise ValueError("Fixture providers are allowed only in development")
        if not isinstance(data, dict) or set(data) != {"video", "pages"} or not isinstance(data["pages"], dict):
            raise ValueError("Fixture needs video metadata and token-keyed pages")
        self.metadata = Video.model_validate(data["video"])
        self.pages = {token: CommentPage.model_validate(page) for token, page in data["pages"].items()}
        if "" not in self.pages or any(not isinstance(token, str) for token in self.pages):
            raise ValueError("Fixture pages must include the first empty-token page")

    @classmethod
    def from_file(cls, settings, path):
        path = Path(path)
        if path.stat().st_size > settings.worker.maximum_result_bytes:
            raise ValueError("Fixture file exceeds the worker byte limit")
        return cls(settings, json.loads(path.read_text(encoding="utf-8")))

    def video(self, video_id):
        if video_id != self.metadata.id:
            raise ProviderError("VIDEO_NOT_FOUND")
        return self.metadata

    def comments(self, video_id, page_token, max_results):
        self.video(video_id)
        page = self.pages.get(page_token or "")
        if page is None:
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        # A fixture must model the exact requested page size; never hide pagination
        # mistakes by silently truncating a response and losing its remaining rows.
        if len(page.comments) > max_results:
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        return page
