from pathlib import Path

from src.Sentiment_analysis.entity.entity import DataIngestionConfig, DataValidationConfig
from src.Sentiment_analysis.utils.common import read_yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ConfigurationManager:
    """Turn YAML settings into typed configurations for each stage."""

    def __init__(self, config_path=None, params_path=None, project_root=PROJECT_ROOT):
        self.project_root = Path(project_root).resolve()
        self.config = read_yaml(config_path or self.project_root / "config/config.yaml")
        self.params_path = Path(params_path or self.project_root / "config/params.yaml")

    def _path(self, value):
        path = (self.project_root / value).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError(f"Configured path must stay inside the project: {value}")
        return path

    def get_data_ingestion_config(self):
        dataset = self.config["dataset"]
        settings = self.config["data_ingestion"]
        return DataIngestionConfig(
            project_root=self.project_root,
            raw_data_file=self._path(dataset["raw_path"]),
            dvc_file=self._path(dataset["dvc_file"]),
            report_file=self._path(settings["report_file"]),
            expected_sha256=dataset["source"]["sha256"],
            expected_size_bytes=int(dataset["source"]["size_bytes"]),
            dataset_name=dataset["name"],
            source_revision=dataset["source"]["revision"],
            restore_with_dvc=bool(settings["restore_with_dvc"]),
        )

    def get_data_validation_config(self):
        dataset = self.config["dataset"]
        settings = self.config["data_validation"]
        columns = dataset["columns"]
        return DataValidationConfig(
            raw_data_file=self._path(dataset["raw_path"]),
            ingestion_report=self._path(self.config["data_ingestion"]["report_file"]),
            report_file=self._path(settings["report_file"]),
            quarantine_file=self._path(settings["quarantine_file"]),
            expected_sha256=dataset["source"]["sha256"],
            encoding=dataset["encoding"],
            required_columns=tuple(settings["required_columns"]),
            text_column=columns["text"],
            title_column=columns["video_title"],
            sentiment_column=columns["sentiment"],
            target_column=columns["target"],
            label_mapping={int(key): value for key, value in dataset["label_mapping"].items()},
        )
