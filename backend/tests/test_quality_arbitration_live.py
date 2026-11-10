"""Offline Terra arbitration contracts, promotion gates and real-call boundary.

Mocks are limited to the provider transport; no real API credits are consumed.
"""
import json
import os
from time import perf_counter
import unittest
from unittest.mock import patch

from quality_errors import (
    QualityArbitrationUnavailable, QualityRegenerationError,
)
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
        "stem_claims": [
            {"claim": "The question mentions Albert Einstein",
             "anchor": "Albert Einstein", "verdict": "supported",
             "reason": "The named person appears in the stem."},
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
            (dict(stem_claims=[
                {"claim": "The named river exists", "anchor": "river",
                 "verdict": "uncertain", "reason": "No independent confirmation"}
            ]), "unsupported_stem_claim"),
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

    def test_false_distractor_is_not_a_false_question_premise(self):
        """Real P2: rook castling/mashed potatoes distractors must stay choices."""
        good = verdict()
        self.assertTrue(is_suitable(
            good, correct_answer="B", bonus=True
        )[0])
        # The wrong answer's negative assessment is already represented
        # by choices[A]="no"; it MUST NOT appear in stem_claims.
        self.assertEqual(good.choices["A"], "no")
        self.assertEqual(len(good.stem_claims), 1)
        from tagging.quality_skill.arbiter import stem_claims_anchored
        self.assertTrue(stem_claims_anchored(
            good, "In what year was Albert Einstein born?"
        ))
        fake_option_claim = verdict(stem_claims=[
            {"claim": "Rice is not used for mashed potatoes",
             "anchor": "Rice", "verdict": "false",
             "reason": "This is about a distractor, not the stem"}
        ])
        self.assertFalse(stem_claims_anchored(
            fake_option_claim,
            "Which ingredient is used for mashed potatoes?"
        ))

    def test_arbiter_schema_has_separate_verbatim_stem_evidence(self):
        from output_contracts import transport_contract, OutputContract
        name, schema = transport_contract(OutputContract.ARBITRATION)
        self.assertEqual(name, "quiz_quality_arbitration_v2")
        self.assertIn("stem_claims", schema["required"])
        self.assertNotIn("claims", schema["properties"])
        self.assertIn("anchor", schema["properties"]["stem_claims"]["items"]["required"])

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
        self.assertEqual(report["contract_version"], "arbitration_v2")
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

    def test_bonus_too_easy_requires_terra_when_below_score_threshold(self):
        """Q16 subjective too_easy goes to Terra; unavailable fails closed."""
        q = sample_question().model_copy(update={"difficulty": 15})
        review = sample_critic(difficulty="too_easy", distractors=0, knowledge=1)
        with patch.dict(os.environ, {
            "QUIZ_QUALITY_ARBITER_MODE": "on",
        }), patch("tagging.review.LLMClient") as reviewer, patch(
            "tagging.quality_skill.arbiter.arbitrate",
            return_value=(False, None, "unavailable"),
        ) as terra:
            reviewer.return_value.responses.create.return_value = ProviderResult(
                json.dumps(review.model_dump(mode="json")), Completion.COMPLETE,
                "completed"
            )
            with self.assertRaises(QualityArbitrationUnavailable):
                check_tagged_quality(
                    q, language="Russian", requested_difficulty=15,
                    category="Geography", neighbors=[],
                    mode=QualityMode.ENFORCE,
                    deadline=perf_counter()+30, timeout_seconds=12,
                )
        self.assertLess(q._review_report["details"]["assessment"]["total"], 3.25)
        self.assertEqual(q._review_report["decision"], "arbitration_unavailable")
        self.assertFalse(is_strict_too_easy_reserve(q))
        terra.assert_called_once()

    def test_primary_accepted_bonus_arbiter_can_veto(self):
        q = sample_question().model_copy(update={"difficulty": 15})
        review = sample_critic(difficulty="appropriate", distractors=0, knowledge=0)
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "on"}), patch(
            "tagging.review.LLMClient"
        ) as reviewer, patch(
            "tagging.quality_skill.arbiter.arbitrate",
            return_value=(False, {
                "checker_type": "ai_terra_arbiter",
                "decision": "reject",
            }, "unsupported_stem_claim")
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

    def test_missing_terra_verdict_is_not_a_content_rejection(self):
        q = sample_question().model_copy(update={"difficulty": 15})
        review = sample_critic(difficulty="appropriate", distractors=0, knowledge=0)
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "on"}), patch(
            "tagging.review.LLMClient"
        ) as reviewer, patch(
            "tagging.quality_skill.arbiter.arbitrate",
            return_value=(False, None, "unavailable"),
        ):
            reviewer.return_value.responses.create.return_value = ProviderResult(
                json.dumps(review.model_dump(mode="json")), Completion.COMPLETE,
                "completed",
            )
            with self.assertRaises(QualityArbitrationUnavailable) as raised:
                check_tagged_quality(
                    q, language="Russian", requested_difficulty=15,
                    category="Geography", neighbors=[],
                    mode=QualityMode.ENFORCE,
                    deadline=perf_counter()+30, timeout_seconds=12,
                    shadow={"would_arbitrate": True},
                )
        self.assertEqual(raised.exception.reason, "unavailable")
        self.assertEqual(q._review_report["decision"], "arbitration_unavailable")

    def test_arbiter_unavailable_aborts_paid_retry_loop_after_one_attempt(self):
        from fastapi import HTTPException
        from api import generate_valid_question
        q = sample_question()
        with patch.dict(os.environ, {
            "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_QUALITY_MODE": "enforce",
            "AI_LOCAL_LEAK_MODE": "off",
            "QUIZ_QUALITY_ARBITER_MODE": "on",
        }), patch("api.create_question", return_value=q.model_dump()) as generator, patch(
            "tagging.review.check_tagged_quality",
            side_effect=QualityArbitrationUnavailable("unavailable")
        ) as reviewer, patch("api.sleep") as sleep:
            with self.assertRaises(HTTPException) as failed:
                generate_valid_question(
                    q.difficulty, "English", "General knowledge",
                    tag_history=[], tag_context=[], too_easy_reserve=[],
                )
        self.assertEqual(failed.exception.status_code, 503)
        self.assertEqual(generator.call_count, 1)
        self.assertEqual(reviewer.call_count, 1)
        sleep.assert_not_called()

    def test_invalid_claim_anchor_is_candidate_rejection_not_a_false_fact(self):
        from tagging.quality_skill.arbiter import stem_claims_anchored
        q = sample_question()
        invalid = verdict(stem_claims=[{
            "claim": "Rice is not used for potato mash",
            "anchor": "rice", "verdict": "false",
            "reason": "A false option cannot become a false stem.",
        }])
        self.assertFalse(stem_claims_anchored(invalid, q.question))
        with patch.dict(os.environ, {"QUIZ_QUALITY_ARBITER_MODE": "on"}), patch(
            "tagging.quality_skill.arbiter.LLMClient"
        ) as llm:
            llm.return_value.responses.create.return_value = ProviderResult(
                json.dumps(invalid.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            allowed, report, reason = arbitrate(
                q, language="Russian", category="History",
                requested_difficulty=15, shadow=None,
                deadline=perf_counter()+20, timeout_seconds=12,
            )
        self.assertFalse(allowed)
        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["issues"], ["invalid_stem_anchor"])
        self.assertEqual(reason, "invalid_stem_anchor")
        self.assertEqual(llm.return_value.responses.create.call_count, 1)

    def test_carbonate_shortcut_invalid_stem_claim_regenerates_without_503(self):
        from api import generate_valid_question
        from tagging.quality_skill.arbiter import stem_claims_anchored
        from tagging.quality_skill.engine import evaluate_shadow

        q = TaggedQuestion.model_validate({
            "difficulty": 4,
            "question": "Which gas is commonly released when an acid reacts with a carbonate?",
            "answers": {
                "A": "Oxygen", "B": "Hydrogen",
                "C": "Carbon dioxide", "D": "Chlorine",
            },
            "correct_answer": "C",
            "tags": {"entity": ["carbonates", "carbon-dioxide", "acids"],
                     "concept": ["chemical-reactions", "gas-evolution"],
                     "keyword": ["acid-plus-carbonate"]},
            "fact": {"subject": "carbonates",
                     "relation": "reacts-with-acid-produces-gas",
                     "object": "carbon-dioxide"},
        })
        bogus = ArbitrationVerdict.model_validate({
            "premise": "valid",
            "choices": {"A": "no", "B": "no", "C": "yes", "D": "no"},
            "difficulty": "appropriate",
            "answer_leak": False,
            "stem_claims": [{
                "claim": "The correct answer says carbon dioxide",
                "anchor": "Carbon dioxide",
                "verdict": "supported",
                "reason": "This text appears only in an answer choice.",
            }],
            "rationale": "The science question is sound.",
        })
        self.assertFalse(stem_claims_anchored(bogus, q.question))
        with patch.dict(os.environ, {
            "QUIZ_QUALITY_SKILL_MODE": "shadow",
            "QUIZ_QUALITY_ARBITER_MODE": "on",
        }), patch("tagging.quality_skill.arbiter.LLMClient") as llm:
            shadow = evaluate_shadow(q, requested_difficulty=4)
            self.assertTrue(shadow["would_arbitrate"])
            self.assertTrue(any(
                f["code"] == "unique_morphological_shortcut"
                for f in shadow["findings"]
            ))
            llm.return_value.responses.create.return_value = ProviderResult(
                json.dumps(bogus.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            allowed, report, reason = arbitrate(
                q, language="English", category="Chemistry",
                requested_difficulty=4, shadow=shadow,
                deadline=perf_counter()+20, timeout_seconds=12,
            )
        self.assertFalse(allowed)
        self.assertEqual(reason, "invalid_stem_anchor")
        self.assertEqual(report["decision"], "reject")
        self.assertEqual(report["issues"], ["invalid_stem_anchor"])

        # The existing bounded generation loop should retry this
        # unverifiable candidate. Real Terra timeouts must NOT retry.
        with patch.dict(os.environ, {
            "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "60",
            "AI_QUALITY_MODE": "enforce",
            "AI_LOCAL_LEAK_MODE": "off",
        }), patch("api.create_question", return_value=q.model_dump()) as gen, patch(
            "tagging.review.check_tagged_quality",
            side_effect=[QualityRegenerationError(), None]
        ) as review, patch("api.sleep") as sleep:
            result = generate_valid_question(
                4, "English", "Chemistry",
                tag_history=[], tag_context=[], too_easy_reserve=[],
            )
        self.assertEqual(result.correct_answer, "C")
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(review.call_count, 2)
        # A normal quality rejection uses the existing bounded retry
        # backoff; only true arbiter outages terminate without sleeping.
        sleep.assert_called_once()

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
            ("Какое растение обычно используют для изготовления бумаги?",
             {"A": "Хлопок", "B": "Бамбук",
              "C": "Подсолнечник", "D": "Картофель"},
             "B", "possible_multiple_correct:paper_fiber"),
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
