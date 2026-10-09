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


@dataclass(frozen=True)
class DataPreprocessingConfig:
    raw_data_file: Path
    validation_report: Path
    quarantine_file: Path
    train_file: Path
    validation_file: Path
    test_file: Path
    report_file: Path
    encoding: str
    text_column: str
    title_column: str
    target_column: str
    label_mapping: dict[int, str]
    parameters: dict


@dataclass(frozen=True)
class MLflowConfig:
    project_root: Path
    tracking_uri: str
    experiment_name: str
    artifact_location: str
    run_name: str
    user_name: str = "Siddhartha Ganguli"


@dataclass(frozen=True)
class ModelTrainingConfig:
    train_file: Path
    validation_file: Path
    preprocessing_report: Path
    model_file: Path
    report_file: Path
    encoding: str
    text_column: str
    title_column: str
    target_column: str
    label_mapping: dict[int, str]
    parameters: dict
    mlflow: MLflowConfig | None = None


@dataclass(frozen=True)
class ModelEvaluationConfig:
    train_file: Path
    validation_file: Path
    test_file: Path
    preprocessing_report: Path
    model_file: Path
    training_report: Path
    report_file: Path
    metrics_file: Path
    classification_report_file: Path
    confusion_matrix_csv: Path
    confusion_matrix_plot: Path
    error_sample_file: Path
    encoding: str
    text_column: str
    title_column: str
    target_column: str
    label_mapping: dict[int, str]
    parameters: dict
    mlflow: MLflowConfig | None = None
