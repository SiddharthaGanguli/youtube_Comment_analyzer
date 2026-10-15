"""Verify a packaged model on Linux before publishing its current-release pointer.

This function performs inference on handwritten golden examples only. It does
not create AWS clients, read API keys, or fetch YouTube comments.
"""

import json
import math
import os
from pathlib import Path

from src.Sentiment_analysis.config.deployment import runtime_settings
from src.Sentiment_analysis.config.worker import WorkerConfigurationManager


def compare_golden(actual, expected):
    if len(actual) != len(expected) or not expected:
        raise ValueError("Golden prediction count differs")
    for row, saved in zip(actual, expected):
        for key in ("status", "sentiment", "skipped_reason"):
            if row.get(key) != saved.get(key):
                raise ValueError("Golden prediction differs")
        if row.get("status") == "analyzed":
            for label in ("Negative", "Neutral", "Positive"):
                score = row["class_probabilities"][label]
                if not math.isclose(score, saved["class_probabilities"][label], rel_tol=1e-5, abs_tol=1e-6):
                    raise ValueError("Golden model scores differ")


def handler(event, context):
    settings = runtime_settings()
    config = WorkerConfigurationManager(settings,
        image_code_revision=os.environ.get("SERVING_IMAGE_CODE_REVISION")).get_worker_config()
    path = Path(__file__).resolve().parents[3] / "config" / "serving-golden.json"
    if path.stat().st_size > 65536:
        raise ValueError("Golden examples exceed the verification limit")
    golden = json.loads(path.read_text(encoding="utf-8"))
    if golden["model_sha256"] != config.identity.model.sha256:
        raise ValueError("Golden examples belong to another model")
    from src.Sentiment_analysis.services.prediction import PredictionService
    predictor = PredictionService(config.prediction, native_thread_limit=settings.worker.native_thread_limit)
    result = predictor.predict(golden["inputs"])
    compare_golden(result["comments"], golden["expected"])
    return {"verified": True, "release_id": config.identity.model.release_id,
            "golden_comments": len(golden["inputs"])}
