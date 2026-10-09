"""Measure local sequential inference; this is not an AWS or user load test."""

import argparse
import csv
import json
import math
import os
import platform
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.services.prediction import PredictionService


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Use a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comments", type=positive_integer, default=1000)
    parser.add_argument("--runs", type=positive_integer, default=20)
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / "reports/local_inference_benchmark.json")
    args = parser.parse_args()
    manager = ConfigurationManager()
    config = replace(manager.get_model_prediction_config(), restore_with_dvc=False)
    if args.comments > config.parameters["max_comments"]:
        parser.error("Comment count exceeds the prediction service's configured maximum")
    source = manager.get_data_ingestion_config().raw_data_file
    if not source.is_file():
        parser.error("Raw dataset is missing; restore it before running this offline benchmark")
    comments = []
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if config.text_column not in (reader.fieldnames or []):
            parser.error(f"Dataset is missing {config.text_column}")
        for row in reader:
            comments.append(row[config.text_column])
            if len(comments) == args.comments:
                break
    if len(comments) != args.comments:
        parser.error("Dataset has fewer rows than the requested sample")
    started = time.perf_counter()
    service = PredictionService(config)
    load_seconds = time.perf_counter() - started
    service.predict(comments)  # One warmup; imports and load are not timed below.
    durations = []
    for _ in range(args.runs):
        started = time.perf_counter()
        result = service.predict(comments)
        durations.append(time.perf_counter() - started)
    try:
        import psutil
        rss = psutil.Process().memory_info().rss
    except ImportError:
        rss = None  # Optional process measurement; not a peak-memory estimate.
    report = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Local inference only; not a concurrent-user or AWS benchmark",
        "model_sha256": service.training["model_sha256"],
        "model_bytes": config.model_file.stat().st_size,
        "environment": {"python": platform.python_version(), "os": platform.platform(),
                        "logical_cpus": os.cpu_count()},
        "sample": {"source": f"First {len(comments)} raw dataset {config.text_column} rows",
                   "comments": len(comments),
                   "mean_characters": statistics.mean(len(text or "") for text in comments)},
        "inference": {
            "batch_size": service.batch_size,
            "native_thread_limit": service.training["parameters"]["thread_limit"],
            "warmup_runs": 1, "measured_runs": args.runs,
            "load_seconds_excluding_imports": load_seconds,
            "median_seconds_per_sample": statistics.median(durations),
            "p95_seconds_per_sample": sorted(durations)[math.ceil(0.95 * args.runs) - 1],
            "p95_method": "nearest_rank",
            "maximum_seconds_per_sample": max(durations),
            "rss_bytes_after_runs": rss,
            "last_analyzed": result["summary"]["analyzed"],
            "last_skipped": result["summary"]["skipped"],
        },
        "limitations": [
            "Sequential repeated input; excludes Python import and container startup",
            "Current machine and trained thread limit; differs from the proposed Lambda worker",
            "No YouTube requests, queue, authentication, storage, or network load tested",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved offline benchmark to {args.output}")


if __name__ == "__main__":
    main()
