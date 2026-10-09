import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.Sentiment_analysis.components.data_ingestion import DataIngestion
from src.Sentiment_analysis.entity.entity import DataIngestionConfig


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.content = b"CommentText,Sentiment_label\nhello,1\n"
        self.raw = self.root / "comments.csv"
        self.raw.write_bytes(self.content)
        self.pointer = self.root / "comments.csv.dvc"
        self.pointer.write_text("outs: []\n")
        self.config = DataIngestionConfig(
            self.root, self.raw, self.pointer, self.root / "report.json",
            hashlib.sha256(self.content).hexdigest(), len(self.content), "fixture", "test", True,
        )

    def test_verified_local_input_is_not_downloaded_or_modified(self):
        with patch("src.Sentiment_analysis.components.data_ingestion.subprocess.run") as restore:
            report = DataIngestion(self.config).run()
        restore.assert_not_called()
        self.assertTrue(report["passed"])
        self.assertEqual(self.raw.read_bytes(), self.content)

    def test_corrupt_input_fails_and_replaces_old_success_report(self):
        DataIngestion(self.config).run()
        self.raw.write_bytes(b"x" * len(self.content))
        with self.assertRaisesRegex(ValueError, "checksum"):
            DataIngestion(self.config).run()
        self.assertFalse(json.loads(self.config.report_file.read_text())["passed"])

    def test_missing_input_uses_dvc(self):
        missing_config = replace(self.config, raw_data_file=self.root / "restored.csv")
        with patch("src.Sentiment_analysis.components.data_ingestion.subprocess.run") as restore:
            restore.side_effect = lambda *args, **kwargs: missing_config.raw_data_file.write_bytes(self.content)
            self.assertTrue(DataIngestion(missing_config).run()["passed"])
        self.assertEqual(restore.call_args.args[0][1:4], ["-m", "dvc", "pull"])


if __name__ == "__main__":
    unittest.main()
