import json

import pandas as pd

from logging_.logger_ import Logger
from src.Sentiment_analysis.entity.entity import DataValidationConfig
from src.Sentiment_analysis.utils.common import comparison_key, read_comments, sha256_file, write_json


class DataValidation:
    def __init__(self, config: DataValidationConfig):
        self.config = config
        self.logger = Logger("data_validation.log").get_logger()

    def run(self):
        config = self.config
        report = {"passed": False}
        try:
            ingestion = json.loads(config.ingestion_report.read_text(encoding="utf-8"))
            if not ingestion.get("passed") or ingestion.get("sha256") != config.expected_sha256:
                raise ValueError("A successful ingestion report for this dataset is required")
            checksum = sha256_file(config.raw_data_file)
            if checksum != config.expected_sha256:
                raise ValueError("Raw data changed after ingestion; run ingestion again")

            df = read_comments(config.raw_data_file, config.encoding)
            report.update({"input_sha256": checksum, "rows": len(df), "columns": df.columns.tolist()})
            missing = sorted(set(config.required_columns) - set(df.columns))
            if missing:
                raise ValueError(f"Required columns are missing: {missing}")
            if df.empty:
                raise ValueError("The dataset has no rows")

            numbers = pd.to_numeric(df[config.target_column], errors="coerce")
            expected = numbers.map(config.label_mapping)
            invalid_labels = ~numbers.isin(config.label_mapping)
            mismatched_labels = ~df[config.sentiment_column].str.strip().eq(expected).fillna(False)
            text_key = comparison_key(df[config.text_column])
            title_key = comparison_key(df[config.title_column])
            empty_text = text_key.eq("")
            empty_title = title_key.eq("")

            context = pd.DataFrame({"title": title_key, "text": text_key, "label": expected})
            usable = ~empty_text & ~empty_title & ~invalid_labels & ~mismatched_labels
            label_counts = context.loc[usable].groupby(["title", "text"])["label"].nunique()
            conflicting_pairs = label_counts[label_counts.gt(1)].index
            context_index = pd.MultiIndex.from_frame(context[["title", "text"]])
            conflict_mask = context_index.isin(conflicting_pairs)

            reasons = pd.Series("", index=df.index, dtype="string")
            for mask, reason in [(empty_text, "empty_comment"), (empty_title, "empty_title"),
                                 (invalid_labels | mismatched_labels, "invalid_label"),
                                 (conflict_mask, "conflicting_context_labels")]:
                reasons.loc[mask] += reason + ";"
            quarantined = reasons.ne("")
            quarantine = pd.DataFrame({"source_row": df.index[quarantined],
                                       "reason": reasons[quarantined].str.rstrip(";").values})
            config.quarantine_file.parent.mkdir(parents=True, exist_ok=True)
            quarantine.to_csv(config.quarantine_file, index=False)

            report.update({
                "empty_fields": {column: int(value) for column, value in df.isna().sum().items()},
                "whitespace_only_or_missing_comments": int(empty_text.sum()),
                "whitespace_only_or_missing_titles": int(empty_title.sum()),
                "invalid_numeric_labels": int(invalid_labels.sum()),
                "label_column_disagreement": int(mismatched_labels.sum()),
                "class_counts": {label: int(expected.eq(label).sum()) for label in config.label_mapping.values()},
                "exact_duplicate_rows_beyond_first": int(df.duplicated().sum()),
                "conflicting_title_comment_pairs": len(conflicting_pairs),
                "rows_in_conflicting_pairs": int(conflict_mask.sum()),
                "quarantined_rows": int(quarantined.sum()),
                "quarantine_sha256": sha256_file(config.quarantine_file),
                "labels_manually_verified": False,
            })
            if invalid_labels.any() or mismatched_labels.any():
                raise ValueError("Invalid or inconsistent sentiment labels; inspect the validation report")
            if set(numbers[~quarantined].dropna().astype(int)) != set(config.label_mapping):
                raise ValueError("Usable data must contain every configured sentiment class")

            # Known quality issues are quarantined. Structural violations stop the pipeline.
            report["passed"] = True
            write_json(config.report_file, report)
            self.logger.info("Validated %s rows; %s rows quarantined", len(df), int(quarantined.sum()))
            return report
        except Exception as error:
            report.update({"passed": False, "error": str(error)})
            write_json(config.report_file, report)
            raise
