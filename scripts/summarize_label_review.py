"""Verify the review worksheet's source rows and summarize provisional decisions."""

import csv
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.utils.common import sha256_file, write_json


def summarize_review():
    manager = ConfigurationManager()
    dataset = manager.config["dataset"]
    raw = PROJECT_ROOT / dataset["raw_path"]
    review_path = PROJECT_ROOT / "data/processed/eda/label_review_sample.csv"
    checksum = sha256_file(raw)
    if checksum != dataset["source"]["sha256"]:
        raise ValueError("Raw source checksum differs from config.yaml")
    with review_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    selected = {int(row["source_row"]): row for row in rows}
    if not rows or len(selected) != len(rows):
        raise ValueError("The review must contain unique source rows")
    remaining = set(selected)
    original_columns = ["VideoTitle", "CommentText", "Sentiment", "Sentiment_label"]
    with raw.open(encoding=dataset["encoding"], newline="") as stream:
        for index, original in enumerate(csv.DictReader(stream)):
            if index in remaining:
                if any(original[key] != selected[index][key] for key in original_columns):
                    raise ValueError(f"Review source row {index} differs from the raw data")
                remaining.remove(index)
            if not remaining:
                break
    if remaining:
        raise ValueError("Review source rows are absent from the raw data")
    allowed = set(dataset["label_mapping"].values())
    for row in rows:
        label = row["assistant_sentiment"]
        status = "needs_context" if label == "Uncertain" else "provisional"
        if (label not in allowed | {"Uncertain"} or not row["assistant_review_notes"].strip()
                or row["reviewer_type"] != "AI assistant" or row["assistant_review_status"] != status):
            raise ValueError(f"Incomplete assistant review at source row {row['source_row']}")
        if row["reviewed_sentiment"].strip() and row["reviewed_sentiment"] not in allowed:
            raise ValueError(f"Invalid human label at source row {row['source_row']}")
    decisive = [row for row in rows if row["assistant_sentiment"] != "Uncertain"]
    disagreements = [row for row in decisive if row["assistant_sentiment"] != row["Sentiment"]]
    counts = Counter(row["assistant_sentiment"] for row in rows)
    human_count = sum(bool(row["reviewed_sentiment"].strip()) for row in rows)
    report = {
        "source_verified": True, "source_sha256": checksum,
        "review_path": review_path.relative_to(PROJECT_ROOT).as_posix(),
        "review_sha256": sha256_file(review_path), "sample_rows": len(rows),
        "sampling": "EDA seed-42 sample stratified by supplied class; not population representative",
        "provided_class_counts": dict(Counter(row["Sentiment"] for row in rows)),
        "assistant_reviewed_rows": len(rows), "assistant_class_counts": dict(counts),
        "decisive_assistant_rows": len(decisive), "needs_context_rows": counts["Uncertain"],
        "provisional_disagreements": len(disagreements),
        "agreement_among_decisive_rows": (len(decisive) - len(disagreements)) / len(decisive) if decisive else None,
        "completed_human_review_rows": human_count, "manual_review_complete": human_count == len(rows),
        "interpretation": "Individual AI assistant judgments, made before comparing supplied labels. "
                          "Disagreements are review candidates, not confirmed errors or model accuracy.",
        "raw_labels_modified": False,
        "disagreement_rows": [
            {"source_row": int(row["source_row"]), "provided": row["Sentiment"],
             "assistant": row["assistant_sentiment"], "reason": row["assistant_review_notes"]}
            for row in disagreements
        ],
        "needs_context_source_rows": [int(row["source_row"]) for row in rows if row["assistant_sentiment"] == "Uncertain"],
    }
    output = PROJECT_ROOT / "reports/label_review_report.json"
    write_json(output, report)
    print(f"Verified {len(rows)} source rows; {len(disagreements)} provisional disagreements, "
          f"{counts['Uncertain']} need context, {human_count} human labels. Saved {output.relative_to(PROJECT_ROOT)}")
    return report


if __name__ == "__main__":
    summarize_review()
