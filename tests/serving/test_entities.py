import copy
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from src.Sentiment_analysis.entity.analysis import (
    Analysis, AnalysisInput, AnalysisResult, Comment, ErrorEnvelope, ModelDetails,
    ResultDownload, WorkMessage,
)
from tests.serving.fixtures import EXPIRY, MODEL, TIME, VIDEO_ID, comment, result, status

ROOT = Path(__file__).resolve().parents[2]


class ServingEntityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = yaml.safe_load((ROOT / "docs/backend-contract.yaml").read_text(encoding="utf-8"))

    def validate_contract(self, entity, name):
        schema = {"$ref": f"#/components/schemas/{name}", "components": self.contract["components"]}
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(entity.model_dump(mode="json"))

    def test_supported_urls_share_one_canonical_body(self):
        urls = [f"https://youtu.be/{VIDEO_ID}?t=10", f"https://www.youtube.com/watch?v={VIDEO_ID}&feature=share",
                f"https://m.youtube.com/shorts/{VIDEO_ID}", f"https://youtube.com/live/{VIDEO_ID}/"]
        for url in urls:
            entity = AnalysisInput(video_url=url)
            self.assertEqual(entity.video_id, VIDEO_ID)
            self.assertEqual(entity.video_url, f"https://www.youtube.com/watch?v={VIDEO_ID}")
            self.validate_contract(entity, "AnalysisInput")

    def test_bad_urls_and_request_fields_fail(self):
        for url in [f"http://youtube.com/watch?v={VIDEO_ID}", f"https://youtube.com.evil.example/watch?v={VIDEO_ID}",
                    f"https://user:password@youtube.com/watch?v={VIDEO_ID}",
                    f"https://youtube.com:8443/watch?v={VIDEO_ID}", "https://youtube.com/watch?v=short",
                    f"https://youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}", "file:///tmp/comments"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                AnalysisInput(video_url=url)
        for extra in [{"max_comments": True}, {"max_comments": "1000"}, {"max_comments": 1001}, {"include_replies": True}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                AnalysisInput(video_url=f"https://youtu.be/{VIDEO_ID}", **extra)

    def test_queued_partial_failed_and_cached_statuses(self):
        queued = Analysis.model_validate(status())
        self.validate_contract(queued, "Analysis")
        for state in ["SUCCEEDED", "PARTIAL", "FAILED"]:
            data = status()
            data.update(state=state, result_available=state != "FAILED", cache_hit=state != "FAILED",
                        partial_reason="UPSTREAM_TIMEOUT" if state == "PARTIAL" else None,
                        failure={"code": "COMMENTS_DISABLED", "message": "Comments disabled", "retryable": False} if state == "FAILED" else None)
            # Completed shared work may have finished before this owner's reference.
            data["deadline_at"] = "2026-10-12T09:30:00Z"
            self.validate_contract(Analysis.model_validate(data), "Analysis")
        invalid = status()
        invalid["result_available"] = True
        with self.assertRaises(ValueError):
            Analysis.model_validate(invalid)

    def test_utc_serialization_and_naive_timestamp_rejection(self):
        data = status()
        data["created_at"] = "2026-10-12T15:30:00+05:30"
        self.assertEqual(Analysis.model_validate(data).model_dump(mode="json")["created_at"], TIME)
        for timestamp in ["2026-10-12T10:00:00", 1791799200]:
            data["created_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                Analysis.model_validate(data)

    def test_empty_and_nonempty_results_match_the_contract(self):
        for empty in [False, True]:
            entity = AnalysisResult.model_validate(result(empty))
            self.validate_contract(entity, "AnalysisResult")
            self.assertEqual(len(entity.comments), entity.summary.received)
            self.assertEqual(AnalysisResult.model_validate_json(entity.model_dump_json()), entity)

    def test_invalid_predictions_are_rejected(self):
        for change in [{"sentiment_label": True}, {"sentiment_label": 0}, {"confidence": float("nan")},
                       {"confidence": 0.9}, {"class_probabilities": {"Negative": 0.5, "Neutral": 0.5, "Positive": 0.5}},
                       {"skipped_reason": "empty_text"}]:
            data = comment()
            data.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                Comment.model_validate(data)

    def test_skipped_predictions_stay_null_and_oversized_text_is_omitted(self):
        skipped = comment()
        skipped.update(text=None, normalized_text=None, status="skipped", sentiment=None,
                       sentiment_label=None, confidence=None, class_probabilities=None,
                       skipped_reason="text_too_long")
        entity = Comment.model_validate(skipped)
        self.validate_contract(entity, "Comment")
        for change in [{"text": "still stored"}, {"confidence": 0.2}, {"needs_review": True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                Comment.model_validate({**skipped, **change})
        for reason, normalized in [("empty_text", ""), ("no_known_features", "qzxvplmnb")]:
            valid = {**skipped, "text": normalized, "normalized_text": normalized, "skipped_reason": reason}
            self.validate_contract(Comment.model_validate(valid), "Comment")
            invalid = {**valid, "normalized_text": "unexpected text" if not normalized else ""}
            with self.assertRaises(ValueError):
                Comment.model_validate(invalid)

    def test_rows_and_summaries_cannot_disagree(self):
        changes = [
            ("summary", "received", 2), ("summary", "coverage", 0.5),
            ("summary", "needs_review", 1), ("sampling", "sample_start", None),
            ("sampling", "sample_end", "2026-10-12T11:00:00Z"),
        ]
        for section, key, value in changes:
            data = result()
            data[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                AnalysisResult.model_validate(data)
        data = result()
        data["comments"][0]["needs_review"] = True
        data["summary"]["needs_review"] = 1
        with self.assertRaisesRegex(ValueError, "model threshold"):
            AnalysisResult.model_validate(data)
        data = result()
        data["comments"][0]["index"] = 5
        with self.assertRaises(ValueError):
            AnalysisResult.model_validate(data)
        data = result()
        data["owner_subject"] = "must-not-be-in-shared-result"
        with self.assertRaises(ValueError):
            AnalysisResult.model_validate(data)
        data = result()
        data["comments"].append(comment(1))
        data["comments"][1]["id"] = data["comments"][0]["id"]
        data["summary"].update(received=2, analyzed=2)
        data["summary"]["sentiment_counts"]["Positive"] = 2
        with self.assertRaisesRegex(ValueError, "Repeated comment IDs"):
            AnalysisResult.model_validate(data)

    def test_download_error_model_metadata_and_queue_shapes(self):
        download = ResultDownload(url="https://fixture.s3.us-east-1.amazonaws.com/result?signature=fixture",
                                  expires_at=EXPIRY, sha256="1" * 64, bytes=100,
                                  content_type="application/json", schema_version=1)
        self.assertNotIn("signature=fixture", repr(download))
        self.validate_contract(download, "ResultDownload")
        error = ErrorEnvelope(error={"code": "INVALID_INPUT", "message": "Invalid URL",
                                     "retryable": False, "request_id": "fixture-request"})
        self.validate_contract(error, "ErrorEnvelope")
        self.assertNotIn("reset_at", error.model_dump(mode="json")["error"])
        quota = ErrorEnvelope(error={"code": "YOUTUBE_QUOTA_EXHAUSTED", "message": "Daily allowance spent",
                                     "retryable": True, "request_id": "fixture-request", "reset_at": EXPIRY})
        self.validate_contract(quota, "ErrorEnvelope")
        with self.assertRaises(ValueError):
            ErrorEnvelope(error={"code": "YOUTUBE_QUOTA_EXHAUSTED", "message": "Quota spent",
                                 "retryable": True, "request_id": "fixture-request"})
        metadata = ModelDetails(model=copy.deepcopy(MODEL), type="TF-IDF + LogisticRegression",
                                labels={"0": "Negative", "1": "Neutral", "2": "Positive"},
                                confidence_definition="Highest class score, not calibrated correctness probability",
                                evaluation={"test_accuracy": 0.71, "test_macro_f1": 0.71, "test_rows": 100,
                                            "labels_human_confirmed": False, "context": "Fixture supplied-label agreement"})
        self.validate_contract(metadata, "ModelDetails")
        WorkMessage(work_id="2a571d3b-40c4-45c2-96bc-6a89e33c9255", release_id=MODEL["release_id"], dispatch_generation=0)


if __name__ == "__main__":
    unittest.main()
