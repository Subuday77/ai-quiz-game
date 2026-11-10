"""Private helper debug contract: disabled by default, complete and stable."""
import json
import os
import unittest
from unittest.mock import patch

from platinum_helper.core import TOPICS
from platinum_helper.debug import _emit, enabled, log, skill_groups


class PlatinumHelperDebugTests(unittest.TestCase):
    def test_disabled_without_explicit_local_opt_in(self):
        with patch.dict(os.environ, {"QUIZ_HELPER_DEBUG": "false"}):
            with patch.object(log, "debug") as write:
                _emit("helper_test", correct_key="B")
                write.assert_not_called()
            self.assertFalse(enabled())

    def test_enabled_json_is_machine_readable(self):
        with patch.dict(os.environ, {"QUIZ_HELPER_DEBUG": "true"}):
            with self.assertLogs("quiz.helper.debug", level="DEBUG") as captured:
                _emit("helper_test", correct_key="B", selected_answer_correct=False)
            record = json.loads(captured.output[0].split("HELPER_DEBUG: ", 1)[1])
            self.assertEqual(record["event"], "helper_test")
            self.assertEqual(record["correct_key"], "B")
            self.assertIs(record["selected_answer_correct"], False)
            self.assertIn("timestamp", record)

    def test_restored_round_debug_identifies_original_helper_without_purchase(self):
        from types import SimpleNamespace
        from platinum_helper.debug import active_round

        saved = SimpleNamespace(
            game_id="round-from-db", language="Russian",
            phase="main", question_issued=True,
            helper={
                "id": "original-purchase", "name": "Luiz Gustavo Pacheco",
                "price": 1, "refunded": False, "consultations_remaining": 3,
            },
        )
        with patch.dict(os.environ, {"QUIZ_HELPER_DEBUG": "true"}):
            with self.assertLogs("quiz.helper.debug", level="DEBUG") as captured:
                active_round(saved)
                active_round(None)
        rows = [
            json.loads(line.split("HELPER_DEBUG: ", 1)[1])
            for line in captured.output
        ]
        self.assertEqual([r["event"] for r in rows],
                         ["helper_active_round_lookup", "helper_active_round_lookup"])
        self.assertEqual(rows[0]["run_id"], "round-from-db")
        self.assertEqual(rows[0]["run_language"], "Russian")
        self.assertEqual(rows[0]["helper_id"], "original-purchase")
        self.assertEqual(rows[0]["helper_price_platinum"], 1)
        self.assertIs(rows[0]["question_issued"], True)
        self.assertIs(rows[1]["found"], False)
        self.assertTrue(all("correct_key" not in row for row in rows))

    def test_all_twenty_levels_included_even_for_legacy_profile(self):
        # Legacy purchases may predate a competency area: it defaults to MEDIUM.
        groups = skill_groups({"skills": {"History": "HIGH", "Geography": "LOW"}})
        self.assertEqual(groups["HIGH"], ["History"])
        self.assertEqual(groups["LOW"], ["Geography"])
        self.assertEqual(len(groups["MEDIUM"]), len(TOPICS) - 2)
        self.assertEqual(set(sum(groups.values(), [])), set(TOPICS))


if __name__ == "__main__":
    unittest.main()
