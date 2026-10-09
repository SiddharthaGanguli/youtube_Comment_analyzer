import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.Sentiment_analysis.components.data_validation import DataValidation
from src.Sentiment_analysis.entity.entity import DataValidationConfig
from src.Sentiment_analysis.utils.common import sha256_file, write_json


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.df = pd.DataFrame([
            ["one", "bad", "Negative", "0"],
            ["two", "NA", "Neutral", "1"],
            ["three", "good", "Positive", "2"],
        ], columns=["VideoTitle", "CommentText", "Sentiment", "Sentiment_label"])

    def run_validation(self, df):
        raw = self.root / "raw.csv"
        df.to_csv(raw, index=False)
        digest = sha256_file(raw)
        ingestion_report = self.root / "ingestion.json"
        write_json(ingestion_report, {"passed": True, "sha256": digest})
        config = DataValidationConfig(raw, ingestion_report, self.root / "validation.json",
                                      self.root / "quarantine.csv", digest, "utf-8", tuple(self.df.columns),
                                      "CommentText", "VideoTitle", "Sentiment", "Sentiment_label",
                                      {0: "Negative", 1: "Neutral", 2: "Positive"})
        return DataValidation(config).run()

    def test_literal_na_is_a_usable_comment(self):
        report = self.run_validation(self.df)
        self.assertTrue(report["passed"])
        self.assertEqual(report["quarantined_rows"], 0)

    def test_conflicts_and_blank_text_are_quarantined(self):
        extra = pd.DataFrame([
            ["conflict", "same", "Negative", "0"],
            ["CONFLICT", " SAME ", "Positive", "2"],
            ["blank", "  ", "Neutral", "1"],
        ], columns=self.df.columns)
        report = self.run_validation(pd.concat([self.df, extra], ignore_index=True))
        self.assertEqual(report["conflicting_title_comment_pairs"], 1)
        self.assertEqual(report["quarantined_rows"], 3)

    def test_inconsistent_label_stops_the_stage(self):
        self.df.loc[0, "Sentiment_label"] = "2"
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.run_validation(self.df)
        self.assertFalse(json.loads((self.root / "validation.json").read_text())["passed"])

    def test_missing_schema_column_stops_the_stage(self):
        with self.assertRaisesRegex(ValueError, "columns are missing"):
            self.run_validation(self.df.drop(columns="CommentText"))
