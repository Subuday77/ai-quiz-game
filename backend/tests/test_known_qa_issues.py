"""Reproducible QA regressions from actual browser runs, no DB mutation."""
import unittest
from types import SimpleNamespace

from tagging.quality_skill.known_issues import known_qa_issue, _KNOWN_ITEMS


class KnownQualityIssuesTests(unittest.TestCase):
    def test_all_six_exact_problematic_items_are_excluded(self):
        for code, stem, keyed in _KNOWN_ITEMS:
            with self.subTest(issue=code):
                question = SimpleNamespace(
                    question=stem,
                    answers={"A": "Unrelated", "B": keyed},
                    correct_answer="B",
                )
                self.assertEqual(known_qa_issue(question), code)
                # A correction must be reviewed normally, never blindly blocked.
                corrected = SimpleNamespace(
                    question=stem + " (independently clarified)",
                    answers=question.answers, correct_answer="B",
                )
                self.assertIsNone(known_qa_issue(corrected))

    def test_matching_requires_actual_keyed_option(self):
        stem = _KNOWN_ITEMS[0][1]
        item = SimpleNamespace(question=stem,
                               answers={"A":"Из яблока", "B":"Из груши"},
                               correct_answer="B")
        self.assertIsNone(known_qa_issue(item))
        self.assertIsNone(known_qa_issue(SimpleNamespace(
            question="Other question", answers={"A":"Из яблока"},
            correct_answer="A",
        )))


if __name__ == "__main__":
    unittest.main()
