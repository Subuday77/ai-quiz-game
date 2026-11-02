"""Offline correctness-skill shadow tests; no provider, web or DB required."""
import os
import unittest
from unittest.mock import patch

from tagging.contract import TaggedQuestion
from tagging.quality_skill.config import SkillSettings
from tagging.quality_skill.engine import evaluate_shadow


def candidate(stem, answers, *, correct="B", difficulty=9):
    return TaggedQuestion.model_validate({
        "difficulty": difficulty,
        "question": stem,
        "answers": answers,
        "correct_answer": correct,
        "tags": {
            "entity": ["shadow-fixture"], "concept": ["quality-regressions"],
            "keyword": [],
        },
        "fact": {"subject": "shadow-fixture", "relation": "checked-answer",
                 "object": "named-answer"},
    })


class ShadowSkillTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "QUIZ_QUALITY_SKILL_MODE": "shadow",
        }))
        self.zircon = candidate(
            "Какой минерал является главным источником циркония и используется "
            "для датирования горных пород?",
            {"A": "Рутил", "B": "Циркон", "C": "Ильменит", "D": "Апатит"},
        )
        self.virginal = candidate(
            "Какой клавишный инструмент с прямоугольным корпусом связан с Англией?",
            {"A": "спинет", "B": "верджинел", "C": "клавикорд", "D": "мюзелер"},
            difficulty=13,
        )

    def test_morphological_zircon_matches_one_option(self):
        with patch("tagging.quality_skill.engine.debug_event") as debug:
            r = evaluate_shadow(self.zircon, requested_difficulty=9)
        lexical = [f for f in r["findings"] if f["tool"] == "lexical"]
        self.assertEqual(len(lexical), 1)
        self.assertEqual(lexical[0]["code"], "unique_morphological_shortcut")
        self.assertEqual(lexical[0]["evidence"]["option"], "B")
        self.assertEqual(lexical[0]["evidence"]["common_prefix"], "цирко")
        self.assertTrue(r["would_arbitrate"])
        self.assertFalse(r["arbiter_called"])
        self.assertEqual(r["decision"], "observe_only")
        self.assertEqual(debug.call_count, 4)  # Three tools plus summary.

    def test_morphological_no_match_or_nonunique_match(self):
        clean = self.zircon.model_copy(update={
            "question": "Какой минерал сохраняет уран в кристаллической решетке?"
        })
        self.assertFalse(any(f["tool"] == "lexical"
                             for f in evaluate_shadow(clean, requested_difficulty=9)["findings"]))
        duplicate = self.zircon.model_copy(update={
            "answers": {**self.zircon.answers, "A": "Цирконит"}
        })
        self.assertFalse(any(f["tool"] == "lexical"
                             for f in evaluate_shadow(duplicate, requested_difficulty=9)["findings"]))

    def test_curated_type_subtype_is_relation_not_automatic_rejection(self):
        r = evaluate_shadow(self.virginal, requested_difficulty=13)
        rel = [f for f in r["findings"] if f["tool"] == "taxonomy"]
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0]["code"], "curated_subtype_overlap")
        self.assertEqual(rel[0]["evidence"]["parent_option"], "B")
        self.assertEqual(rel[0]["evidence"]["child_option"], "D")
        self.assertGreater(rel[0]["relation_confidence"], rel[0]["confidence"])
        self.assertTrue(r["would_arbitrate"])
        self.assertEqual(r["decision"], "observe_only")

    def test_unlisted_taxonomy_does_not_guess_facts(self):
        neutral = self.virginal.model_copy(update={
            "answers": {"A": "спинет", "B": "верджинел",
                        "C": "клавикорд", "D": "челеста"}
        })
        r = evaluate_shadow(neutral, requested_difficulty=13)
        self.assertFalse([f for f in r["findings"] if f["tool"] == "taxonomy"])

    def test_superlatives_flag_verification_without_claiming_falsehood(self):
        kings = candidate(
            "Какой клуб выиграл Кубок Стэнли, набрав меньше всего очков "
            "среди всех участников плей-офф?",
            {"A": "Los Angeles Kings", "B": "Devils", "C": "Canadiens",
             "D": "Flames"},
            correct="A", difficulty=14,
        )
        r = evaluate_shadow(kings, requested_difficulty=14)
        facts = [f for f in r["findings"] if f["tool"] == "fact"]
        self.assertTrue(facts)
        self.assertEqual(facts[0]["evidence"]["claim_truth"], "unknown")
        self.assertEqual(facts[0]["evidence"]["verification"], "not_performed")
        self.assertTrue(r["would_arbitrate"])

    def test_unremarkable_stem_does_not_request_arbitration(self):
        q = candidate(
            "Кто написал стихотворение «Бородино»?",
            {"A": "Тютчев", "B": "Лермонтов",
             "C": "Некрасов", "D": "Блок"},
            difficulty=2,
        )
        r = evaluate_shadow(q, requested_difficulty=2)
        self.assertFalse(r["findings"])
        self.assertFalse(r["would_arbitrate"])
        self.assertEqual(r["risk_score"], 0)

    def test_weights_are_independent_and_do_not_alter_publication(self):
        baseline = evaluate_shadow(self.zircon, requested_difficulty=9)
        with patch.dict(os.environ, {"QUIZ_QUALITY_LEXICAL_PENALTY_WEIGHT": "5.0"}):
            updated = evaluate_shadow(self.zircon, requested_difficulty=9)
        self.assertGreater(updated["tool_scores"]["lexical"],
                           baseline["tool_scores"]["lexical"])
        self.assertEqual(updated["tool_scores"]["taxonomy"],
                         baseline["tool_scores"]["taxonomy"])
        self.assertEqual(updated["tool_scores"]["fact"],
                         baseline["tool_scores"]["fact"])
        self.assertEqual(updated["decision"], "observe_only")
        self.assertFalse(updated["arbiter_called"])

    def test_off_mode_is_quiet_and_returns_without_network(self):
        with patch.dict(os.environ, {"QUIZ_QUALITY_SKILL_MODE": "off"}), patch(
                "tagging.quality_skill.engine.debug_event") as debug:
            r = evaluate_shadow(self.zircon, requested_difficulty=9)
        self.assertEqual(r["mode"], "off")
        self.assertFalse(r["would_arbitrate"])
        debug.assert_not_called()

    def test_configuration_bounds_fail_locally(self):
        cases = [
            {"QUIZ_QUALITY_SKILL_MODE": "enforce"},
            {"QUIZ_QUALITY_LEXICAL_MIN_PREFIX": "banana"},
            {"QUIZ_QUALITY_ARBITRATION_MIN_CONFIDENCE": "1.1"},
            {"QUIZ_QUALITY_TAXONOMY_PENALTY_WEIGHT": "-1"},
        ]
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    SkillSettings.from_env()


if __name__ == "__main__":
    unittest.main()
