"""Synthetic metadata only: none of these values describes a deployed release."""

import copy

MODEL = {
    "release_id": "sr_" + "1" * 64,
    "sha256": "2" * 64,
    "preprocessing_sha256": "3" * 64,
    "review_confidence_threshold": 0.6,
    "result_schema_version": 1,
}
TIME = "2026-10-12T10:00:00Z"
EXPIRY = "2026-10-13T10:00:00Z"
VIDEO_ID = "dQw4w9WgXcQ"


def sampling(empty=True):
    return {"scope": "public_top_level_comments", "order": "time", "include_replies": False,
            "requested_max_comments": 1000, "has_more": False,
            "sample_start": None if empty else TIME, "sample_end": None if empty else TIME}


def status():
    return {"analysis_id": "2a571d3b-40c4-45c2-96bc-6a89e33c9255", "video_id": VIDEO_ID,
            "state": "QUEUED", "created_at": TIME, "updated_at": TIME,
            "deadline_at": "2026-10-12T10:30:00Z", "expires_at": EXPIRY,
            "cache_hit": False, "model": copy.deepcopy(MODEL), "sampling": sampling(),
            "progress": {"fetched": 0, "duplicates_removed": 0, "analyzed": 0, "skipped": 0},
            "result_available": False, "partial_reason": None, "failure": None}


def comment(index=0):
    return {"index": index, "id": f"fixture-{index}", "author": "Fixture author",
            "published_at": TIME, "likes": 2, "text": "A useful explanation!",
            "normalized_text": "A useful explanation!", "status": "analyzed",
            "sentiment": "Positive", "sentiment_label": 2, "confidence": 0.8,
            "class_probabilities": {"Negative": 0.1, "Neutral": 0.1, "Positive": 0.8},
            "needs_review": False, "skipped_reason": None}


def result(empty=False):
    rows = [] if empty else [comment()]
    count = len(rows)
    return {"schema_version": 1, "state": "SUCCEEDED", "fetched_at": TIME, "expires_at": EXPIRY,
            "video": {"id": VIDEO_ID, "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
                      "title": "Fixture video", "channel_title": "Fixture channel",
                      "published_at": TIME, "total_comment_count": None},
            "sampling": sampling(empty), "duplicates_removed": 0, "model": copy.deepcopy(MODEL),
            "confidence_definition": "Highest class score, not calibrated correctness probability",
            "summary": {"received": count, "analyzed": count, "skipped": 0,
                        "coverage": float(bool(count)),
                        "sentiment_counts": {"Negative": 0, "Neutral": 0, "Positive": count},
                        "skipped_by_reason": {"empty_text": 0, "no_known_features": 0, "text_too_long": 0},
                        "needs_review": 0},
            "partial_reason": None, "comments": rows}
