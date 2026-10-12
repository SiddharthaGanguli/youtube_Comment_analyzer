"""Turn a bounded sample into the public result using the shared predictor."""

import math
from collections import Counter

from src.Sentiment_analysis.entity.analysis import (
    AnalysisResult, Comment, CONFIDENCE_DEFINITION, LABELS, Sampling,
)
from src.Sentiment_analysis.entity.worker import CapturedComment, CommentPage, WorkerCheckpoint
from src.Sentiment_analysis.integrations.fixture_comments import ProviderError


def update_checkpoint(checkpoint, **updates):
    # model_copy skips validation; checkpoint transitions must be checked again.
    return WorkerCheckpoint.model_validate({**checkpoint.model_dump(), **updates})


class AnalysisProcessor:
    def __init__(self, settings, predictor):
        self.settings, self.predictor = settings, predictor

    def accept_page(self, checkpoint, page, limit):
        if not isinstance(page, CommentPage) or len(page.comments) > limit:
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        seen = {row.id for row in (*checkpoint.predictions, *checkpoint.pending)}
        pending, duplicates = list(checkpoint.pending), checkpoint.duplicates_removed
        for row in page.comments:
            if row.id in seen:
                duplicates += 1
                continue
            seen.add(row.id)
            large = row.text is not None and len(row.text) > self.settings.worker.maximum_comment_characters
            pending.append(CapturedComment(**{**row.model_dump(), "text": None if large else row.text},
                                           text_too_long=large))
        tokens = (*checkpoint.page_tokens, checkpoint.next_page_token or "")
        if page.next_page_token is not None and page.next_page_token in tokens:
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        return update_checkpoint(checkpoint, pending=tuple(pending), duplicates_removed=duplicates,
                                 fetched=checkpoint.fetched + len(page.comments), page_tokens=tokens,
                                 next_page_token=page.next_page_token, call_attempts=0)

    def finish_fetch(self, checkpoint, requested, *, partial_reason=None):
        maximum_pages = math.ceil(requested / self.settings.youtube.page_size)
        more = checkpoint.next_page_token is not None
        reason = partial_reason
        if not reason and more and checkpoint.fetched < requested and len(checkpoint.page_tokens) >= maximum_pages:
            reason = "PAGE_LIMIT_REACHED"
        return update_checkpoint(checkpoint, fetch_complete=True, has_more=more if not reason else True,
                                 partial_reason=reason)

    def infer_batch(self, checkpoint):
        batch = checkpoint.pending[:self.settings.worker.batch_size]
        eligible = [row for row in batch if not row.text_too_long]
        predicted = self.predictor.predict([row.text for row in eligible]) if eligible else {"comments": []}
        rows = predicted["comments"]
        if (not isinstance(rows, list) or len(rows) != len(eligible) or
                any(type(row.get("index")) is not int for row in rows) or
                {row["index"] for row in rows} != set(range(len(eligible)))):
            raise ValueError("Prediction rows must match their original input indices")
        by_index = {row["index"]: row for row in rows}
        safe_index, completed = 0, list(checkpoint.predictions)
        for captured in batch:
            if captured.text_too_long:
                prediction = {"text": None, "normalized_text": None, "status": "skipped", "sentiment": None,
                              "sentiment_label": None, "confidence": None, "class_probabilities": None,
                              "needs_review": False, "skipped_reason": "text_too_long"}
            else:
                prediction = dict(by_index[safe_index])
                safe_index += 1
                if prediction["text"] != captured.text:
                    raise ValueError("Prediction text differs from its original input")
            prediction.pop("index", None)
            metadata = captured.model_dump(exclude={"text", "text_too_long"})
            completed.append(Comment(index=len(completed), **metadata, **prediction))
        return update_checkpoint(checkpoint, pending=checkpoint.pending[len(batch):], predictions=tuple(completed))

    @staticmethod
    def sampling(checkpoint, requested):
        rows = (*checkpoint.predictions, *checkpoint.pending)
        dates = [row.published_at for row in rows]
        return Sampling(scope="public_top_level_comments", order="time", include_replies=False,
                        requested_max_comments=requested, has_more=checkpoint.has_more,
                        sample_start=min(dates) if dates else None, sample_end=max(dates) if dates else None)

    def result(self, checkpoint, work):
        if not checkpoint.fetch_complete or checkpoint.pending:
            raise ValueError("Finish fetching and infer every unique row before publishing")
        rows = checkpoint.predictions
        analyzed = [row for row in rows if row.status == "analyzed"]
        sentiments = Counter(row.sentiment for row in analyzed)
        skips = Counter(row.skipped_reason for row in rows if row.status == "skipped")
        return AnalysisResult(
            schema_version=1, state="PARTIAL" if checkpoint.partial_reason else "SUCCEEDED",
            fetched_at=checkpoint.fetched_at, expires_at=work.expires_at, video=checkpoint.video,
            sampling=self.sampling(checkpoint, work.input.max_comments), duplicates_removed=checkpoint.duplicates_removed,
            model=work.model, confidence_definition=CONFIDENCE_DEFINITION, partial_reason=checkpoint.partial_reason,
            summary={"received": len(rows), "analyzed": len(analyzed), "skipped": len(rows) - len(analyzed),
                     "coverage": len(analyzed) / len(rows) if rows else 0.0,
                     "sentiment_counts": {label: sentiments[label] for label in LABELS.values()},
                     "skipped_by_reason": {r: skips[r] for r in ("empty_text", "no_known_features", "text_too_long")},
                     "needs_review": sum(row.needs_review for row in analyzed)}, comments=rows)
