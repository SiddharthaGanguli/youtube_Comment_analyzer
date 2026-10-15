import copy
import unittest

from src.Sentiment_analysis.workers.verify_release import compare_golden


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"status": "analyzed", "sentiment": "Positive", "skipped_reason": None,
                      "class_probabilities": {"Negative": .1, "Neutral": .2, "Positive": .7}},
                     {"status": "skipped", "sentiment": None, "skipped_reason": "empty_text"}]

    def test_equivalent_predictions_allow_small_platform_roundoff(self):
        actual = copy.deepcopy(self.rows)
        actual[0]["class_probabilities"]["Positive"] += 1e-7
        compare_golden(actual, self.rows)

    def test_wrong_label_score_skips_and_row_count_are_rejected(self):
        variants = [self.rows[:1], []]
        for field, value in [("sentiment", "Negative"), ("status", "skipped")]:
            changed = copy.deepcopy(self.rows); changed[0][field] = value; variants.append(changed)
        changed = copy.deepcopy(self.rows); changed[0]["class_probabilities"]["Positive"] = .6; variants.append(changed)
        changed = copy.deepcopy(self.rows); changed[1]["skipped_reason"] = "no_known_features"; variants.append(changed)
        for actual in variants:
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                compare_golden(actual, self.rows)
