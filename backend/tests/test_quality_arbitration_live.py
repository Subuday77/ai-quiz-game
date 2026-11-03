"""Offline Terra arbitration contracts, promotion gates and real-call boundary.

Mocks are limited to the provider transport; no real API credits are consumed.
"""
import json
import os
from time import perf_counter
import unittest
from unittest.mock import patch

from quality_errors import QualityRegenerationError
from quality_schema import QualityMode
from provider_result import Completion, ProviderResult
from tagging.contract import TaggedQuestion
from tagging.quality_skill.arbiter import (
    ArbitrationVerdict, arbitrate, is_suitable,
)
from tagging.quality_skill.engine import evaluate_shadow
from tagging.review import check_tagged_quality, is_strict_too_easy_reserve
from test_tagged_dedup import candidate as sample_question
from test_tagged_dynamic_scoring import critic as sample_critic


def verdict(**changes):
    base = {
        "premise": "valid",
        "choices": {k: "yes" if k == "B" else "no" for k in "ABCD"},
        "difficulty": "appropriate",
        "answer_leak": False,
        "claims": [
            {"claim": "Location and event in the question",
             "verdict": "supported", "reason": "The cited association is valid."},
            {"claim": "Correct choice is unique",
             "verdict": "supported", "reason": "Competing choices do not fit."},
        ],
        "rationale": "All independent checks passed.",
    }
    return ArbitrationVerdict.model_validate({**base, **changes})


class ArbitrationPolicyTests(unittest.TestCase):
    def test_disputed_options_claims_leaks_and_bonus_difficulty_rejected(self):
        cases = [
            (dict(premise="uncertain"), "premise_uncertain"),
            (dict(choices={"A":"yes","B":"yes","C":"no","D":"no"}), "options_not_unique"),
            (dict(choices={"A":"no","B":"yes","C":"uncertain","D":"no"}), "uncertain_option"),
            (dict(answer_leak=True), "answer_leak"),
            (dict(claims=[
                {"claim": "The named river exists", "verdict": "uncertain",
                 "reason": "No independent confirmation"}
            ]), "unsupported_claim"),
            (dict(difficulty="too_easy"), "bonus_too_easy"),
        ]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                allowed, why = is_suitable(
                    verdict(**changes), correct_answer="B", bonus=True
                )
                self.assertFalse(allowed)
                self.assertEqual(why, reason)
        self.assertTrue(is_suitable(
            verdict(difficulty="too_easy"), correct_answer="B", bonus=False
        )[0])

    def test_default_off_never_calls_model(self):
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "off"}), patch(
            "tagging.quality_skill.arbiter.LLMClient"
        ) as llm:
            result = arbitrate(
                sample_question(), language="Russian", category="Geography",
                requested_difficulty=15, shadow=None,
                deadline=perf_counter()+20, timeout_seconds=12,
            )
        self.assertEqual(result, (True, None, "disabled"))
        llm.assert_not_called()

    def test_bonus_calls_real_provider_boundary_with_strict_contract(self):
        from output_contracts import OutputContract
        with patch.dict(os.environ, {
            "QUIZ_QUALITY_ARBITER_MODE": "on",
            "QUIZ_QUALITY_ARBITER_MODEL_ID": "gpt-6.1-terra",
            "QUIZ_QUALITY_ARBITER_TIMEOUT_SECONDS": "10",
        }), patch("tagging.quality_skill.arbiter.LLMClient") as llm, patch(
            "tagging.quality_skill.arbiter.debug_event"
        ) as debug:
            llm.return_value.responses.create.return_value = ProviderResult(
                json.dumps(verdict().model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            accepted, report, why = arbitrate(
                sample_question(), language="Russian", category="Geography",
                requested_difficulty=15, shadow={"findings": []},
                deadline=perf_counter()+20, timeout_seconds=12,
            )
        self.assertTrue(accepted)
        self.assertEqual(why, "accepted")
        self.assertEqual(report["model"], "gpt-6.1-terra")
        self.assertEqual(llm.call_args.kwargs["role"], "arbiter")
        self.assertEqual(llm.call_args.kwargs["structured_output_mode"], "strict")
        self.assertEqual(llm.return_value.responses.create.call_args.kwargs[
            "output_contract"], OutputContract.ARBITRATION)
        self.assertIn("ALL material", llm.return_value.responses.create.call_args.kwargs[
            "instructions"])
        self.assertTrue(any(x.args[0] == "quality_arbitration_debug"
                            for x in debug.call_args_list))

    def test_flagged_normal_question_is_arbitrated_and_failure_is_closed(self):
        q = sample_question()
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "on"}), patch(
            "tagging.quality_skill.arbiter.LLMClient"
        ) as llm:
            llm.return_value.responses.create.side_effect = TimeoutError()
            accepted, report, reason = arbitrate(
                q, language="Russian", category="Geography",
                requested_difficulty=9,
                shadow={"would_arbitrate": True, "findings": []},
                deadline=perf_counter()+20, timeout_seconds=12,
            )
        self.assertFalse(accepted)
        self.assertIsNone(report)
        self.assertEqual(reason, "unavailable")

    def test_unflagged_normal_question_skips_expensive_call(self):
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "on"}), patch(
            "tagging.quality_skill.arbiter.LLMClient"
        ) as llm:
            accepted, report, reason = arbitrate(
                sample_question(), language="Russian", category="History",
                requested_difficulty=7, shadow={"would_arbitrate": False},
                deadline=perf_counter()+20, timeout_seconds=12,
            )
        self.assertEqual((accepted, report, reason), (
            True, None, "not_triggered"
        ))
        llm.assert_not_called()

    def test_strict_bonus_too_easy_rejected_below_score_threshold(self):
        q = sample_question().model_copy(update={"difficulty": 15})
        review = sample_critic(difficulty="too_easy", distractors=0, knowledge=1)
        with patch.dict(os.environ, {
            "QUIZ_QUALITY_ARBITER_MODE": "off",
        }), patch("tagging.review.LLMClient") as reviewer, patch(
            "tagging.quality_skill.arbiter.arbitrate"
        ) as terra:
            reviewer.return_value.responses.create.return_value = ProviderResult(
                json.dumps(review.model_dump(mode="json")), Completion.COMPLETE,
                "completed"
            )
            with self.assertRaises(QualityRegenerationError):
                check_tagged_quality(
                    q, language="Russian", requested_difficulty=15,
                    category="Geography", neighbors=[],
                    mode=QualityMode.ENFORCE,
                    deadline=perf_counter()+30, timeout_seconds=12,
                )
        self.assertLess(q._review_report["details"]["assessment"]["total"], 3.25)
        self.assertEqual(q._review_report["decision"], "reject")
        self.assertFalse(is_strict_too_easy_reserve(q))
        terra.assert_not_called()

    def test_primary_accepted_bonus_arbiter_can_veto(self):
        q = sample_question().model_copy(update={"difficulty": 15})
        review = sample_critic(difficulty="appropriate", distractors=0, knowledge=0)
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "on"}), patch(
            "tagging.review.LLMClient"
        ) as reviewer, patch(
            "tagging.quality_skill.arbiter.arbitrate",
            return_value=(False, None, "unsupported_claim")
        ) as terra:
            reviewer.return_value.responses.create.return_value = ProviderResult(
                json.dumps(review.model_dump(mode="json")), Completion.COMPLETE,
                "completed",
            )
            with self.assertRaises(QualityRegenerationError):
                check_tagged_quality(
                    q, language="Russian", requested_difficulty=15,
                    category="Geography", neighbors=[],
                    mode=QualityMode.ENFORCE,
                    deadline=perf_counter()+30, timeout_seconds=12,
                    shadow={"would_arbitrate": True, "findings": []},
                )
        self.assertEqual(q._review_report["decision"], "reject")
        terra.assert_called_once()

    def test_savarin_rome_river_origin_and_calvados_signals(self):
        scenarios = [
            ("Какой автор связан с блюдом саварен и книгой о вкусе?",
             {"A":"Жан Антельм Брийя-Саварен","B":"Огюст Эскофье",
              "C":"Мари Карем","D":"Жан Ботеро"}, "A",
             "unique_exact_name_component"),
            ("Какой город был столицей Древней Римской империи?",
             {"A":"Афины","B":"Рим","C":"Карфаген","D":"Александрия"},
             "B", "unique_morphological_shortcut"),
            ("Столица какого государства расположена на берегах реки Чуруйунгбо?",
             {"A":"Бутан","B":"Лесото","C":"Тибет","D":"Монголия"},
             "A", "named_river_location"),
            ("Какая головоломка традиционно считается происходящей из игры Хуа Жун Дао?",
             {"A":"Пятнашки","B":"Ханойская башня",
              "C":"Магический квадрат","D":"Игра в солдат"},
             "D", "historical_derivation_claim"),
            ("Из какого фрукта традиционно делают кальвадос?",
             {"A":"Из груши","B":"Из яблока",
              "C":"Из сливы","D":"Из абрикоса"}, "B",
             "possible_multiple_correct:calvados_fruit"),
        ]
        with patch.dict(os.environ, {"QUIZ_QUALITY_SKILL_MODE": "shadow"}):
            for stem, answers, correct, expected in scenarios:
                with self.subTest(expected=expected):
                    q = TaggedQuestion.model_validate({
                        "difficulty": 15, "question": stem,
                        "answers": answers, "correct_answer": correct,
                        "tags": {"entity": ["test-subject"],
                                 "concept": ["test-topic"], "keyword": []},
                        "fact": {"subject": "test-subject",
                                 "relation": "tested", "object": "demo"},
                    })
                    data = evaluate_shadow(q, requested_difficulty=15)
                    self.assertIn(expected, [f["code"] if f["tool"] != "fact"
                                  or not f["code"].startswith("needs_source_verification:")
                                  else f["code"].split(":",1)[1]
                                  for f in data["findings"]])
                    self.assertTrue(data["would_arbitrate"])


if __name__ == "__main__":
    unittest.main()
