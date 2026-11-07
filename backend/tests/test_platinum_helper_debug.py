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

    def test_all_twenty_levels_included_even_for_legacy_profile(self):
        # Legacy purchases may predate a competency area: it defaults to MEDIUM.
        groups = skill_groups({"skills": {"History": "HIGH", "Geography": "LOW"}})
        self.assertEqual(groups["HIGH"], ["History"])
        self.assertEqual(groups["LOW"], ["Geography"])
        self.assertEqual(len(groups["MEDIUM"]), len(TOPICS) - 2)
        self.assertEqual(set(sum(groups.values(), [])), set(TOPICS))


if __name__ == "__main__":
    unittest.main()
