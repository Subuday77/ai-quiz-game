"""Offline tests for the post-game v3 audit summarizer."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_quality_audit import read_audits, summarize


class AuditSummaryTests(unittest.TestCase):
    def test_non_audit_log_lines_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "server.log"
            path.write_text(
                "INFO: server started\n"
                + json.dumps({"event": "quality_review_score", "contract": "v3",
                              "score": 3, "critical": "none", "penalties": {
                                  "distractors": 3, "knowledge": 0,
                                  "suitability": 0, "wording": 0}}) + "\n"
                + json.dumps({"event": "quality_review_score", "contract": "v2",
                              "score": 8}) + "\n", encoding="utf-8"
            )
            audits = read_audits(path)
            self.assertEqual(len(audits), 1)
            _, critical, totals, thresholds = summarize(audits)
            self.assertEqual(critical["none"], 1)
            self.assertEqual(totals["distractors"], 3)
            self.assertEqual(thresholds, {2: 1, 3: 1, 4: 0, 5: 0})

    def test_critical_counts_even_under_threshold(self):
        audits = [{"score": 0, "critical": "regenerate", "penalties": {
            "distractors": 0, "knowledge": 0, "suitability": 0, "wording": 0}}]
        _, _, _, thresholds = summarize(audits)
        self.assertEqual(thresholds, {2: 1, 3: 1, 4: 1, 5: 1})


if __name__ == "__main__":
    unittest.main()
