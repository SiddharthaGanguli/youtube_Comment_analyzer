from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataIngestionConfig:
    project_root: Path
    raw_data_file: Path
    dvc_file: Path
    report_file: Path
    expected_sha256: str
    expected_size_bytes: int
    dataset_name: str
    source_revision: str
    restore_with_dvc: bool


@dataclass(frozen=True)
class DataValidationConfig:
    raw_data_file: Path
    ingestion_report: Path
    report_file: Path
    quarantine_file: Path
    expected_sha256: str
    encoding: str
    required_columns: tuple[str, ...]
    text_column: str
    title_column: str
    sentiment_column: str
    target_column: str
    label_mapping: dict[int, str]
