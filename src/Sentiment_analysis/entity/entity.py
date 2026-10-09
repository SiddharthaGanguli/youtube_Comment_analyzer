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
