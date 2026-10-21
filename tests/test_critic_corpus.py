"""Offline validation of the versioned critic rubric and benchmark corpus."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "backend" / "skills" / "quiz_quality_review"


class CriticCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rubric = json.loads((SKILL / "rubric.json").read_text(encoding="utf-8"))
        cls.rows = [json.loads(line) for line in (SKILL / "examples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_all_thirty_cases_unique_and_complete(self):
        self.assertEqual(len(self.rows), 30)
        self.assertEqual(len({r["id"] for r in self.rows}), 30)
        self.assertEqual({r["generator"] for r in self.rows}, {"lightning", "gpt-5.4-mini", "gpt-5.4"})
        for row in self.rows:
            self.assertEqual(set(row["candidate"]["answers"]), set("ABCD"))
            self.assertIn(row["candidate"]["correct_answer"], "ABCD")
            self.assertEqual(row["requested_difficulty"], 8)

    def test_labels_distinguish_drafts_from_provisional(self):
        for row in self.rows:
            gold = row["gold"]
            self.assertIn(gold["status"], {"provisional", "reviewed_draft"})
            if gold["status"] == "provisional":
                self.assertIsNone(gold["decision"])
            else:
                self.assertIn(gold["decision"], {"accept", "regenerate", "escalate"})
            self.assertTrue(gold["notes"])

    def test_rubric_keeps_runtime_unchanged(self):
        self.assertFalse(self.rubric["runtime_behavior_changed"])
        self.assertEqual(self.rubric["status"], "evaluation_only")


if __name__ == "__main__":
    unittest.main()
