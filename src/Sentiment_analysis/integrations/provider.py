"""The worker-facing provider contract. Exceptions carry only safe fixed codes."""

from typing import Protocol

from src.Sentiment_analysis.entity.analysis import Video
from src.Sentiment_analysis.entity.worker import CommentPage


class ProviderError(Exception):
    def __init__(self, code, *, retryable=False, circuit_reason=None, retry_after_seconds=None):
        if circuit_reason not in {None, "quota", "credentials"}:
            raise ValueError("Unsupported provider circuit reason")
        if retry_after_seconds is not None and (type(retry_after_seconds) is not int
                                                or not 1 <= retry_after_seconds <= 86400):
            raise ValueError("Provider retry delay must be between one second and one day")
        self.code, self.retryable = code, retryable
        self.circuit_reason, self.retry_after_seconds = circuit_reason, retry_after_seconds
        super().__init__(code)


class CommentProvider(Protocol):
    is_fixture: bool

    def video(self, video_id: str) -> Video: ...

    def comments(self, video_id: str, page_token: str | None, max_results: int) -> CommentPage: ...
