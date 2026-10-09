import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import yaml


def read_yaml(path):
    with Path(path).open(encoding="utf-8") as file:
        content = yaml.safe_load(file)
    if not isinstance(content, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return content


def write_json(path, content):
    """Replace a report only after its complete contents have been written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(content, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary_path, path)


def sha256_file(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def read_comments(path, encoding="utf-8"):
    # Literal comments such as NA and NULL are text, not missing-value markers.
    return pd.read_csv(path, dtype="string", keep_default_na=False, na_values=[""], encoding=encoding)


def comparison_key(text):
    return text.fillna("").str.replace(r"\s+", " ", regex=True).str.strip().str.casefold()


def check_split_overlap(splits, text_column, title_column):
    """Fail if any two splits share titles, comment text, or source rows."""
    names = list(splits)
    for position, left_name in enumerate(names):
        left = splits[left_name]
        for right_name in names[position + 1:]:
            right = splits[right_name]
            for column in [text_column, title_column, "source_row"]:
                left_keys = comparison_key(left[column]) if column != "source_row" else left[column].astype(str)
                right_keys = comparison_key(right[column]) if column != "source_row" else right[column].astype(str)
                if set(left_keys) & set(right_keys):
                    raise ValueError(f"{column} overlaps between {left_name} and {right_name}")


def load_verified_split(path, metadata, encoding, text_column, title_column, target_column, labels):
    if sha256_file(path) != metadata["sha256"]:
        raise ValueError(f"Split checksum changed: {path}")
    frame = read_comments(path, encoding)
    required = {"source_row", text_column, title_column, target_column}
    if not required.issubset(frame.columns) or len(frame) != metadata["rows"]:
        raise ValueError(f"Split schema or row count changed: {path}")
    numbers = pd.to_numeric(frame[target_column], errors="coerce")
    if not numbers.isin(labels).all() or set(numbers) != set(labels):
        raise ValueError(f"Invalid or missing classes in split: {path}")
    if comparison_key(frame[text_column]).eq("").any() or comparison_key(frame[title_column]).eq("").any():
        raise ValueError(f"Empty text or title in split: {path}")
    if not frame.source_row.is_unique:
        raise ValueError(f"Duplicate source rows in split: {path}")
    frame[target_column] = numbers.astype(int)
    return frame


def classification_metrics(targets, predictions, labels, probabilities=None):
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, f1_score, log_loss

    metrics = {
        "accuracy": float(accuracy_score(targets, predictions)),
        "balanced_accuracy": float(balanced_accuracy_score(targets, predictions)),
        "f1_macro": float(f1_score(targets, predictions, labels=labels, average="macro", zero_division=0)),
        "f1_weighted": float(f1_score(targets, predictions, labels=labels, average="weighted", zero_division=0)),
    }
    if probabilities is not None:
        metrics["log_loss"] = float(log_loss(targets, probabilities, labels=labels))
    return metrics
