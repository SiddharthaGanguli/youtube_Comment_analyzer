import json
import math

import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

from logging_.logger_ import Logger
from src.Sentiment_analysis.entity.entity import DataPreprocessingConfig
from src.Sentiment_analysis.utils.common import (
    check_split_overlap, comparison_key, read_comments, sha256_file, write_json,
)
from src.Sentiment_analysis.utils.text import normalize_text


class DataPreprocessing:
    def __init__(self, config: DataPreprocessingConfig):
        self.config = config
        self.logger = Logger("data_preprocessing.log").get_logger()

    def _normalize(self, values):
        form = self.config.parameters["unicode_normalization"]
        return values.fillna("").map(lambda text: normalize_text(text, form)).astype("string")

    def run(self):
        config = self.config
        params = config.parameters
        report = {"passed": False}
        try:
            fractions = [float(params[name]) for name in ["train_size", "validation_size", "test_size"]]
            if not all(0 < value < 1 for value in fractions) or not math.isclose(sum(fractions), 1):
                raise ValueError("The three split fractions must be positive and sum to one")
            if params["language_policy"] != "keep_all":
                raise ValueError("V1 supports the keep_all language policy; language filtering needs a separate review")
            if params["unicode_normalization"] not in {"NFC", "NFKC"}:
                raise ValueError("Use NFC or NFKC for Unicode normalization")

            validation = json.loads(config.validation_report.read_text(encoding="utf-8"))
            if not validation.get("passed"):
                raise ValueError("A successful validation report is required")
            input_sha256 = sha256_file(config.raw_data_file)
            if input_sha256 != validation["input_sha256"]:
                raise ValueError("Raw input changed after validation")
            if sha256_file(config.quarantine_file) != validation["quarantine_sha256"]:
                raise ValueError("The quarantine file changed after validation")

            df = read_comments(config.raw_data_file, config.encoding)
            quarantine = pd.read_csv(config.quarantine_file)
            if not quarantine.source_row.is_unique or not quarantine.source_row.isin(df.index).all():
                raise ValueError("Quarantine source rows do not match the raw dataset")
            remaining = df.loc[~df.index.isin(quarantine.source_row)]
            cleaned = remaining.drop_duplicates().copy()
            duplicates_removed = len(remaining) - len(cleaned)
            cleaned.insert(0, "source_row", cleaned.index)
            cleaned[config.text_column] = self._normalize(cleaned[config.text_column])
            cleaned[config.title_column] = self._normalize(cleaned[config.title_column])
            cleaned[config.target_column] = pd.to_numeric(cleaned[config.target_column]).astype(int)
            cleaned = cleaned[["source_row", config.title_column, config.text_column, config.target_column]]

            text_key = comparison_key(cleaned[config.text_column])
            title_key = comparison_key(cleaned[config.title_column])
            empty = text_key.eq("") | title_key.eq("")
            contexts = pd.DataFrame({"title": title_key, "text": text_key,
                                     "label": cleaned[config.target_column]})
            counts = contexts.groupby(["title", "text"])["label"].nunique()
            extra_conflicts = pd.MultiIndex.from_frame(contexts[["title", "text"]]).isin(counts[counts.gt(1)].index)
            cleaned = cleaned.loc[~empty & ~extra_conflicts].copy()
            groups = comparison_key(cleaned[config.title_column])
            if groups.nunique() < 3:
                raise ValueError("At least three distinct title groups are needed for three splits")

            seed = int(params["random_state"])
            train_val_idx, test_idx = next(GroupShuffleSplit(n_splits=1, test_size=fractions[2],
                                                            random_state=seed).split(cleaned, groups=groups))
            train_val = cleaned.iloc[train_val_idx]
            train_idx, val_idx = next(GroupShuffleSplit(
                n_splits=1, test_size=fractions[1] / (fractions[0] + fractions[1]), random_state=seed,
            ).split(train_val, groups=comparison_key(train_val[config.title_column])))
            splits = {"train": train_val.iloc[train_idx].copy(),
                      "validation": train_val.iloc[val_idx].copy(), "test": cleaned.iloc[test_idx].copy()}

            # Keep the test set fixed; remove repeated text from the lower-priority splits.
            purged = {}
            heldout_keys = set(comparison_key(splits["test"][config.text_column]))
            for name in ["validation", "train"]:
                frame = splits[name]
                overlap = comparison_key(frame[config.text_column]).isin(heldout_keys)
                purged[name] = int(overlap.sum())
                splits[name] = frame.loc[~overlap].copy()
                heldout_keys.update(comparison_key(splits[name][config.text_column]))
            check_split_overlap(splits, config.text_column, config.title_column)

            split_reports = {}
            paths = {"train": config.train_file, "validation": config.validation_file, "test": config.test_file}
            for name, frame in splits.items():
                if frame.empty or set(frame[config.target_column]) != set(config.label_mapping):
                    raise ValueError(f"{name} must contain every sentiment class after overlap removal")
                paths[name].parent.mkdir(parents=True, exist_ok=True)
                frame.to_csv(paths[name], index=False, encoding=config.encoding)
                split_reports[name] = {
                    "rows": len(frame), "distinct_titles": int(comparison_key(frame[config.title_column]).nunique()),
                    "class_counts": {config.label_mapping[label]: int(frame[config.target_column].eq(label).sum())
                                     for label in config.label_mapping},
                    "sha256": sha256_file(paths[name]),
                }
            report.update({
                "passed": True, "input_sha256": input_sha256, "parameters": params,
                "input_rows": len(df), "quarantined_rows_removed": len(quarantine),
                "exact_duplicate_rows_removed_after_quarantine": duplicates_removed,
                "empty_rows_after_normalization": int(empty.sum()),
                "additional_conflicting_rows_after_normalization": int(extra_conflicts.sum()),
                "cross_split_comment_rows_removed": purged, "splits": split_reports,
                "cross_split_title_overlap": 0, "cross_split_comment_overlap": 0,
            })
            write_json(config.report_file, report)
            self.logger.info("Prepared split rows: %s", {name: len(frame) for name, frame in splits.items()})
            return report
        except Exception as error:
            report.update({"passed": False, "error": str(error)})
            write_json(config.report_file, report)
            raise
