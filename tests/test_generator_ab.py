"""Offline tests for paired generator comparison (no live API usage)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from test_openai_generator_ab import (SCENARIOS, as_markdown,
                                       generate_pair, variant_order)


class GeneratorABTests(unittest.TestCase):
    def test_scenarios_cover_early_and_advanced_difficulty(self):
        self.assertEqual(len(SCENARIOS), 12)
        self.assertEqual([level for level, _ in SCENARIOS[:8]], list(range(8)))
        self.assertEqual([level for level, _ in SCENARIOS[-4:]], [8, 10, 12, 15])

    def test_order_is_balanced(self):
        self.assertEqual(variant_order(1), ("v1", "v2"))
        self.assertEqual(variant_order(2), ("v2", "v1"))
        self.assertEqual(variant_order(3), ("v1", "v2"))

    def test_identical_arguments_except_version(self):
        calls = []

        def fake_generator(level, language, category, *, prompt_version):
            calls.append((level, language, category, prompt_version))
            return {
                "difficulty": level, "question": "Какая планета известна кольцами?",
                "answers": {"A": "Земля", "B": "Сатурн",
                            "C": "Марс", "D": "Венера"},
                "correct_answer": "B",
            }

        with patch("test_openai_generator_ab.create_question", side_effect=fake_generator):
            rows = generate_pair(1, 5, "Astronomy and space", "Russian")
        self.assertEqual([row["status"] for row in rows], ["valid", "valid"])
        self.assertEqual([call[:3] for call in calls], [
            (5, "Russian", "Astronomy and space")] * 2)
        self.assertEqual([call[3] for call in calls], ["v1", "v2"])

    def test_report_blinds_version_before_answer_key(self):
        with patch("test_openai_generator_ab.create_question") as generator:
            generator.return_value = {
                "difficulty": 0, "question": "Что светит днём?",
                "answers": {"A": "Луна", "B": "Солнце",
                            "C": "Фонарь", "D": "Лампа"},
                "correct_answer": "B",
            }
            rows = generate_pair(1, 0, "Everyday objects", "Russian")
        report = as_markdown(rows)
        main, key = report.split("## Answer key and prompt versions")
        self.assertIn("Variant A", main)
        self.assertIn("Variant B", main)
        self.assertNotIn("v1", main)
        self.assertNotIn("v2", main)
        self.assertIn("v1", key)
        self.assertIn("v2", key)

    def test_invalid_generated_level_is_recorded_not_hidden(self):
        with patch("test_openai_generator_ab.create_question") as generator:
            generator.return_value = {
                "difficulty": 1, "question": "Какой цвет у травы?",
                "answers": {"A": "Зелёный", "B": "Синий",
                            "C": "Красный", "D": "Жёлтый"},
                "correct_answer": "A",
            }
            rows = generate_pair(1, 3, "Plants", "Russian")
        self.assertEqual([row["status"] for row in rows], ["error", "error"])
        self.assertIn("Returned difficulty", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
