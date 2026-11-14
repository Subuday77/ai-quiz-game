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
        self.assertEqual(lexical[0]["evidence"]["common_prefix"], "циркон")
        self.assertTrue(r["would_arbitrate"])
        self.assertFalse(r["arbiter_called"])
        self.assertEqual(r["decision"], "observe_only")
        self.assertEqual(debug.call_count, 4)  # Three tools plus summary.

    def test_acidic_acid_player_incident_flags_unique_noun_adjective_shortcut(self):
        q = candidate(
            "Which substance turns blue litmus paper red because it is acidic?",
            {"A": "Sodium hydroxide solution",
             "B": "Hydrochloric acid",
             "C": "Ammonia solution",
             "D": "Calcium carbonate"},
            correct="B", difficulty=4,
        )
        report = evaluate_shadow(q, requested_difficulty=4)
        lexical = [f for f in report["findings"] if f["tool"] == "lexical"]
        self.assertEqual(len(lexical), 1)
        self.assertEqual(lexical[0]["code"], "unique_noun_adjective_shortcut")
        self.assertEqual(lexical[0]["evidence"]["question_token"], "acidic")
        self.assertEqual(lexical[0]["evidence"]["option_token"], "acid")
        self.assertEqual(lexical[0]["evidence"]["noun_adjective_family"], "acid")
        self.assertEqual(lexical[0]["evidence"]["option"], "B")
        self.assertTrue(report["would_arbitrate"])
        self.assertIn("unique_noun_adjective_shortcut", report["arbitration_reasons"])
        self.assertFalse(report["arbiter_called"])
        self.assertEqual(report["decision"], "observe_only")
        from local_leak_filter import exact_answer_leak
        self.assertFalse(exact_answer_leak(q), "Shadow signals must not change hard leak gate")

    def test_curated_noun_adjective_pairs_are_exact_and_symmetric(self):
        cases = [
            ("Which reagent is described as acidic?",
             {"A": "Salt", "B": "Acid", "C": "Sugar", "D": "Water"}, "acid"),
            ("Which classification is basic?",
             {"A": "Compound", "B": "Base", "C": "Element", "D": "Ion"}, "base"),
            ("Which material is magnetic?",
             {"A": "Wood", "B": "Magnet", "C": "Plastic", "D": "Rubber"}, "magnet"),
            ("Which adjective describes a base?",
             {"A": "Metallic", "B": "Basic", "C": "Gaseous", "D": "Liquid"}, "base"),
            ("Which adjective describes a magnet?",
             {"A": "Transparent", "B": "Magnetic", "C": "Brittle", "D": "Volatile"},
             "magnet"),
        ]
        for stem, choices, noun in cases:
            with self.subTest(stem=stem):
                report = evaluate_shadow(candidate(stem, choices, correct="B"),
                                         requested_difficulty=9)
                findings = [f for f in report["findings"] if f["tool"] == "lexical"]
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["code"], "unique_noun_adjective_shortcut")
                self.assertEqual(findings[0]["evidence"]["noun_adjective_family"], noun)
                self.assertTrue(report["would_arbitrate"])

    def test_short_pairs_require_one_option_and_do_not_lower_general_prefix_limit(self):
        original = candidate(
            "Which reagent is acidic?",
            {"A": "Sugar", "B": "Hydrochloric acid",
             "C": "Water", "D": "Salt"}, correct="B",
        )
        two_acids = original.model_copy(update={
            "answers": {**original.answers, "A": "Acetic acid"},
        })
        double = evaluate_shadow(two_acids, requested_difficulty=9)
        self.assertFalse([f for f in double["findings"] if f["tool"] == "lexical"])
        # Pair is an exact token relation: no arbitrary short prefixes,
        # e.g. acidity is not identical to the curated adjective acidic.
        no_pair = original.model_copy(update={
            "question": "Which reagent causes acidity?",
        })
        self.assertFalse([f for f in evaluate_shadow(
            no_pair, requested_difficulty=9)["findings"] if f["tool"] == "lexical"])
        unrelated = original.model_copy(update={
            "question": "Which reagent is related to basements?",
        })
        self.assertFalse([f for f in evaluate_shadow(
            unrelated, requested_difficulty=9)["findings"] if f["tool"] == "lexical"])
        # A matching distractor is not a keyed leak or mandatory arbiter call.
        distractor = original.model_copy(update={
            "correct_answer": "C",
        })
        report = evaluate_shadow(distractor, requested_difficulty=9)
        signals = [f for f in report["findings"] if f["tool"] == "lexical"]
        self.assertEqual(signals[0]["code"], "distractor_word_overlap")
        self.assertFalse(report["would_arbitrate"])

    def test_prefixed_root_shortcut_is_shadow_only(self):
        # The actual Q3 incident: a root inside a prefixed answer, rather
        # than at the start of both tokens, escaped the previous detector.
        q = candidate(
            "Какая часть человеческого тела обычно называется коленной чашечкой?",
            {"A": "Лопатка", "B": "Надколенник",
             "C": "Локтевая кость", "D": "Ключица"},
            correct="B", difficulty=2,
        )
        report = evaluate_shadow(q, requested_difficulty=2)
        signals = [f for f in report["findings"] if f["tool"] == "lexical"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["code"], "unique_prefixed_root_shortcut")
        self.assertEqual(signals[0]["evidence"]["common_prefix"], "коленн")
        self.assertEqual(signals[0]["evidence"]["prefix_in_option"], "над")
        self.assertEqual(signals[0]["evidence"]["option"], "B")
        self.assertTrue(report["would_arbitrate"])
        self.assertFalse(report["arbiter_called"])
        self.assertEqual(report["decision"], "observe_only")
        # No changes to the independent, high-confidence publication veto.
        from local_leak_filter import exact_answer_leak
        self.assertFalse(exact_answer_leak(q))

    def test_prefixed_root_requires_one_option_and_long_root(self):
        q = candidate(
            "Какая часть человеческого тела обычно называется коленной чашечкой?",
            {"A": "Подколенная область", "B": "Надколенник",
             "C": "Локтевая кость", "D": "Ключица"},
            correct="B", difficulty=2,
        )
        self.assertFalse([f for f in evaluate_shadow(
            q, requested_difficulty=2)["findings"] if f["tool"] == "lexical"])
        unrelated = candidate(
            "Какая кость защищает сустав при прыжке?",
            {"A": "Лопатка", "B": "Надколенник",
             "C": "Локтевая кость", "D": "Ключица"},
            correct="B", difficulty=2,
        )
        # A generic literal "кость" in the C distractor is a preexisting
        # lexical finding, not a new prefixed-root match.
        self.assertFalse([f for f in evaluate_shadow(
            unrelated, requested_difficulty=2)["findings"]
            if f["code"] == "unique_prefixed_root_shortcut"])

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
