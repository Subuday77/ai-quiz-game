"""Independent, configurable editorial weights and smooth difficulty progress."""
import os
import unittest
from unittest.mock import patch

from tagging.review import TaggedReview, tagged_assessment


def critic(*, difficulty="too_easy", distractors=1, knowledge=2,
           suitability=0, wording=0):
    return TaggedReview.model_validate({
        "premise": "valid",
        "option_assessments": {
            k: {"status": "yes" if k == "B" else "no", "rationale": "examined"}
            for k in "ABCD"
        },
        "facts": "pass", "options": "pass", "language": "pass",
        "leakage": "pass", "difficulty": difficulty, "independent_answer": "B",
        "issue_codes": ["difficulty_mismatch"] if difficulty != "appropriate" else [],
        "distractor_penalty": distractors,
        "knowledge_penalty": knowledge,
        "suitability_penalty": suitability,
        "wording_penalty": wording,
        "rationale": "Synthetic critic editorial assessment",
        "tags_valid": "pass", "fact_valid": "pass",
        "semantic_duplicate": False, "duplicate_match": "",
    })


class DynamicDifficultyTests(unittest.TestCase):
    def setUp(self):
        keys = [
            "QUIZ_QUALITY_SCORE_THRESHOLD",
            "QUIZ_QUALITY_DISTRACTORS_WEIGHT",
            "QUIZ_QUALITY_SUITABILITY_WEIGHT",
            "QUIZ_QUALITY_WORDING_WEIGHT",
            "QUIZ_QUALITY_KNOWLEDGE_UNIT",
            "QUIZ_QUALITY_KNOWLEDGE_CAP",
            "QUIZ_QUALITY_TOO_HARD_UNIT",
            "QUIZ_QUALITY_TOO_HARD_CAP",
            "QUIZ_QUALITY_TOO_EASY_UNIT_START",
            "QUIZ_QUALITY_TOO_EASY_UNIT_END",
            "QUIZ_QUALITY_TOO_EASY_CAP_START",
            "QUIZ_QUALITY_TOO_EASY_CAP_END",
            "QUIZ_QUALITY_TOO_EASY_CURVE",
        ]
        self.enterContext(patch.dict(os.environ, {k: os.environ.get(k, "")
                                                 for k in []}))
        self.enterContext(patch.dict(os.environ, {}, clear=False))
        self.old = {key: os.environ.get(key) for key in keys}
        for key in keys:
            os.environ.pop(key, None)
        self.addCleanup(self._restore, keys)

    def _restore(self, keys):
        for key in keys:
            if self.old[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self.old[key]

    def test_too_easy_increases_smoothly_with_level(self):
        v = critic(difficulty="too_easy", distractors=1, knowledge=2)
        early = tagged_assessment(v, "B", requested_difficulty=0)
        mid = tagged_assessment(v, "B", requested_difficulty=10)
        late = tagged_assessment(v, "B", requested_difficulty=15)
        self.assertAlmostEqual(early["penalties"]["knowledge"], 0.5)
        self.assertGreater(mid["penalties"]["knowledge"], early["penalties"]["knowledge"])
        self.assertLess(mid["penalties"]["knowledge"], late["penalties"]["knowledge"])
        self.assertAlmostEqual(late["penalties"]["knowledge"], 3.0)
        self.assertAlmostEqual(late["total"], 4.0)
        self.assertTrue(late["threshold_preview"]["3.25"])
        self.assertFalse(early["threshold_preview"]["3.25"])

    def test_too_hard_does_not_get_stronger_near_bonus(self):
        v = critic(difficulty="too_hard", knowledge=2)
        a = tagged_assessment(v, "B", requested_difficulty=0)
        b = tagged_assessment(v, "B", requested_difficulty=15)
        self.assertEqual(a["penalties"]["knowledge"], b["penalties"]["knowledge"])
        self.assertAlmostEqual(b["penalties"]["knowledge"], 0.5)

    def test_editorial_components_are_independent(self):
        v = critic(difficulty="appropriate", knowledge=1, distractors=2,
                   suitability=1, wording=1)
        before = tagged_assessment(v, "B", requested_difficulty=15)
        with patch.dict(os.environ, {
            "QUIZ_QUALITY_DISTRACTORS_WEIGHT": "0.5",
            "QUIZ_QUALITY_WORDING_WEIGHT": "2",
            "QUIZ_QUALITY_SCORE_THRESHOLD": "4.5",
        }):
            after = tagged_assessment(v, "B", requested_difficulty=15)
        self.assertEqual(after["penalties"]["distractors"], 1)
        self.assertEqual(after["penalties"]["wording"], 2)
        self.assertEqual(after["penalties"]["knowledge"], before["penalties"]["knowledge"])
        self.assertEqual(after["penalties"]["suitability"], before["penalties"]["suitability"])
        self.assertEqual(after["score_threshold"], 4.5)

    def test_invalid_config_rejected(self):
        v = critic()
        with patch.dict(os.environ, {"QUIZ_QUALITY_TOO_EASY_UNIT_END": "NaN"}):
            with self.assertRaises(ValueError):
                tagged_assessment(v, "B", requested_difficulty=15)


if __name__ == "__main__":
    unittest.main()
