import subprocess
import sys

from logging_.logger_ import Logger
from src.Sentiment_analysis.entity.entity import DataIngestionConfig
from src.Sentiment_analysis.utils.common import sha256_file, write_json


class DataIngestion:
    def __init__(self, config: DataIngestionConfig):
        self.config = config
        self.logger = Logger("data_ingestion.log").get_logger()

    def run(self):
        config = self.config
        restored = False
        try:
            if not config.raw_data_file.is_file():
                if not config.restore_with_dvc:
                    raise FileNotFoundError(f"Raw dataset is missing: {config.raw_data_file}")
                if not config.dvc_file.is_file():
                    raise FileNotFoundError(f"DVC pointer is missing: {config.dvc_file}")
                self.logger.info("Restoring the raw dataset with DVC")
                subprocess.run(
                    [sys.executable, "-m", "dvc", "pull", str(config.dvc_file)],
                    cwd=config.project_root,
                    check=True,
                )
                restored = True

            size = config.raw_data_file.stat().st_size
            if size != config.expected_size_bytes:
                raise ValueError(f"Dataset size differs: expected {config.expected_size_bytes}, got {size}")
            checksum = sha256_file(config.raw_data_file)
            if checksum != config.expected_sha256:
                raise ValueError("Raw dataset checksum does not match config.yaml")

            report = {
                "passed": True,
                "dataset": config.dataset_name,
                "source_revision": config.source_revision,
                "raw_path": config.raw_data_file.relative_to(config.project_root).as_posix(),
                "size_bytes": size,
                "sha256": checksum,
            }
            write_json(config.report_file, report)
            self.logger.info("Dataset verified (%s bytes); restored with DVC: %s", size, restored)
            return report
        except Exception as error:
            write_json(config.report_file, {"passed": False, "error": str(error)})
            raise
