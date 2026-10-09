import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.Sentiment_analysis.components.data_preprocessing import DataPreprocessing
from src.Sentiment_analysis.entity.entity import DataPreprocessingConfig
from src.Sentiment_analysis.utils.common import check_split_overlap, sha256_file, write_json


class PreprocessingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        rows = [[f"video {group}", f"not bad 😊 &amp; group{group} label{label}", label]
                for group in range(30) for label in range(3)]
        # Exact duplicates should be removed without eliminating all repeated phrases.
        rows.append(rows[0])
        self.raw = self.root / "raw.csv"
        pd.DataFrame(rows, columns=["VideoTitle", "CommentText", "Sentiment_label"]).to_csv(self.raw, index=False)
        self.quarantine = self.root / "quarantine.csv"
        pd.DataFrame(columns=["source_row", "reason"]).to_csv(self.quarantine, index=False)
        self.validation = self.root / "validation.json"
        write_json(self.validation, {"passed": True, "input_sha256": sha256_file(self.raw),
                                     "quarantine_sha256": sha256_file(self.quarantine)})
        self.config = DataPreprocessingConfig(
            self.raw, self.validation, self.quarantine, self.root / "train.csv", self.root / "val.csv",
            self.root / "test.csv", self.root / "report.json", "utf-8", "CommentText", "VideoTitle",
            "Sentiment_label", {0: "Negative", 1: "Neutral", 2: "Positive"},
            {"random_state": 42, "train_size": .8, "validation_size": .1, "test_size": .1,
             "unicode_normalization": "NFKC", "language_policy": "keep_all"},
        )

    def test_grouped_splits_preserve_cues_and_are_repeatable(self):
        first = DataPreprocessing(self.config).run()
        second = DataPreprocessing(self.config).run()
        self.assertEqual(first, second)
        self.assertEqual(first["exact_duplicate_rows_removed_after_quarantine"], 1)
        frames = {name: pd.read_csv(path, dtype="string") for name, path in
                  [("train", self.config.train_file), ("val", self.config.validation_file), ("test", self.config.test_file)]}
        check_split_overlap(frames, "CommentText", "VideoTitle")
        self.assertTrue(all(frame.CommentText.str.contains("not bad 😊 & group", regex=False).all()
                            for frame in frames.values()))

    def test_edited_quarantine_stops_preprocessing(self):
        self.quarantine.write_text("source_row,reason\n0,manual edit\n")
        with self.assertRaisesRegex(ValueError, "quarantine file changed"):
            DataPreprocessing(self.config).run()
        self.assertFalse(json.loads(self.config.report_file.read_text())["passed"])

    def test_text_overlap_is_detected_even_across_different_titles(self):
        left = pd.DataFrame({"source_row": [0], "VideoTitle": ["one"], "CommentText": ["same"]})
        right = pd.DataFrame({"source_row": [1], "VideoTitle": ["two"], "CommentText": [" SAME "]})
        with self.assertRaisesRegex(ValueError, "CommentText overlaps"):
            check_split_overlap({"train": left, "test": right}, "CommentText", "VideoTitle")
