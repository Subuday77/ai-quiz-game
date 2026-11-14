"""Spatial constraints must survive both critic and Terra verification.

Mock only the provider boundaries. No real API calls, internet or DB writes.
The original player incident was: "Ural ... south of Volga" with the
unrelated true triple ural-river / flows-into / caspian-sea.
"""
import json
import os
import unittest
from time import perf_counter
from unittest.mock import patch

from provider_result import Completion, ProviderResult
from quality_errors import QualityRegenerationError
from quality_schema import QualityMode
from tagging.quality_skill.arbiter import (
    ArbitrationVerdict, arbitrate, spatial_constraints_covered,
)
from tagging.quality_skill.engine import evaluate_shadow
from tagging.quality_skill.fact_tool import spatial_constraints
from tagging.review import check_tagged_quality
from test_quality_skill_shadow import candidate
from test_tagged_dynamic_scoring import critic


URAL = candidate(
    "Какая река впадает в Каспийское море южнее Волги?",
    {"A": "Дон", "B": "Урал", "C": "Днепр", "D": "Печора"},
    correct="B", difficulty=5,
).model_copy(deep=True)


def spatial_verdict(*, anchor="южнее Волги", conclusion="false",
                    premise="invalid", keyed=False):
    return ArbitrationVerdict.model_validate({
        "premise": premise,
        "choices": {
            key: ("yes" if keyed and key == "B" else "no") for key in "ABCD"
        },
        "difficulty": "appropriate",
        "answer_leak": False,
        "stem_claims": [{
            "claim": "Does the proposed answer satisfy the named direction?",
            "anchor": anchor,
            "verdict": conclusion,
            "reason": ("The Ural delta is north of the Volga delta, "
                       "so it is not south of it.") if conclusion == "false"
                      else "Independently verified the directional relationship.",
        }],
        "rationale": "Check the complete spatial constraint, not just the endpoint.",
    })


class SpatialDetectorTests(unittest.TestCase):
    def test_incident_and_cross_language_comparisons_are_not_dropped(self):
        cases = [
            ("Какая река впадает в Каспийское море южнее Волги?",
             ["южнее Волги"]),
            ("Какой город лежит севернее Москвы?",
             ["севернее Москвы"]),
            ("Какой город расположен к западу от Берлина?",
             ["к западу от Берлина"]),
            ("Which city is west of Berlin?",
             ["west of Berlin"]),
            ("Which river flows south of the Volga?",
             ["south of the Volga"]),
            ("Which capital is north of the Thames and east of London?",
             ["north of the Thames", "east of London"]),
            ("Welche Stadt liegt südlich von München?",
             ["südlich von München"]),
            ("Quelle ville se trouve au nord de Paris?",
             ["au nord de Paris"]),
            ("¿Qué ciudad queda al sur de Madrid?",
             ["al sur de Madrid"]),
        ]
        for stem, expected in cases:
            with self.subTest(stem=stem):
                self.assertEqual(spatial_constraints(stem), expected)

    def test_directional_adjectives_and_noncomparisons_stay_unflagged(self):
        for stem in (
            "Какой город является самым южным в России?",
            "Какое море принимает воды реки Урал?",
            "Какая река впадает в Каспийское море?",
            "Which river flows into the Caspian Sea?",
            "What is the southernmost capital?",
            "Which part of London is south?",
        ):
            with self.subTest(stem=stem):
                self.assertEqual(spatial_constraints(stem), [])

    def test_shadow_logs_flag_and_keep_zero_model_calls(self):
        with patch.dict(os.environ, {"QUIZ_QUALITY_SKILL_MODE": "shadow"}):
            with patch("tagging.quality_skill.engine.debug_event") as events:
                report = evaluate_shadow(URAL, requested_difficulty=5)
        findings = [f for f in report["findings"] if f["tool"] == "fact"]
        self.assertTrue(any(
            f["code"] == "needs_source_verification:spatial_direction_comparison"
            for f in findings
        ))
        self.assertTrue(report["would_arbitrate"])
        self.assertFalse(report["arbiter_called"])
        self.assertEqual(events.call_count, 4)
        self.assertEqual(findings[0]["evidence"]["claim_truth"], "unknown")
        self.assertEqual(findings[0]["evidence"]["constraint"], "южнее Волги")


class MandatoryArbitrationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "QUIZ_QUALITY_ARBITER_MODE": "off",
            "QUIZ_QUALITY_ARBITER_MODEL_ID": "gpt-6.1-terra",
            "QUIZ_QUALITY_ARBITER_TIMEOUT_SECONDS": "10",
        }))

    def test_bad_ural_question_rejected_even_if_generic_arbiter_off(self):
        wrong = spatial_verdict()
        with patch("tagging.quality_skill.arbiter.LLMClient") as llm:
            llm.return_value.responses.create.return_value = ProviderResult(
                json.dumps(wrong.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            accepted, report, why = arbitrate(
                URAL, language="Russian", category="Geography",
                requested_difficulty=5, shadow={"would_arbitrate": False},
                mandatory_spatial=["южнее Волги"],
                deadline=perf_counter() + 20, timeout_seconds=12,
            )
        self.assertFalse(accepted)
        self.assertEqual(why, "premise_invalid")
        self.assertEqual(report["decision"], "reject")
        self.assertEqual(llm.call_count, 1)
        submitted = json.loads(
            llm.return_value.responses.create.call_args.kwargs["input"]
        )
        self.assertEqual(submitted["mandatory_spatial_constraints"], ["южнее Волги"])

    def test_arbiter_must_address_comparison_not_only_true_main_fact(self):
        incomplete = spatial_verdict(
            anchor="Каспийское море", conclusion="supported",
            premise="valid", keyed=True,
        )
        self.assertFalse(spatial_constraints_covered(
            incomplete, ["южнее Волги"]
        ))
        with patch("tagging.quality_skill.arbiter.LLMClient") as llm:
            llm.return_value.responses.create.return_value = ProviderResult(
                json.dumps(incomplete.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            accepted, report, why = arbitrate(
                URAL, language="Russian", category="Geography",
                requested_difficulty=5, shadow=None,
                mandatory_spatial=["южнее Волги"],
                deadline=perf_counter() + 20, timeout_seconds=12,
            )
        self.assertFalse(accepted)
        self.assertEqual(why, "spatial_constraint_unchecked")
        self.assertIsNotNone(report, "Missing mandatory evidence is a content veto, not a 503")
        self.assertEqual(report["issues"], ["spatial_constraint_unchecked"])

    def test_correct_spatial_question_with_explicit_evidence_is_accepted(self):
        question = candidate(
            "Какая столица находится западнее Берлина?",
            {"A": "Москва", "B": "Париж",
             "C": "Варшава", "D": "Прага"},
            correct="B", difficulty=5,
        )
        valid = spatial_verdict(
            anchor="западнее Берлина", conclusion="supported",
            premise="valid", keyed=True,
        )
        with patch("tagging.quality_skill.arbiter.LLMClient") as llm:
            llm.return_value.responses.create.return_value = ProviderResult(
                json.dumps(valid.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            accepted, report, reason = arbitrate(
                question, language="Russian", category="Geography",
                requested_difficulty=5, shadow=None,
                mandatory_spatial=spatial_constraints(question.question),
                deadline=perf_counter() + 20, timeout_seconds=12,
            )
        self.assertTrue(accepted)
        self.assertEqual(reason, "accepted")
        self.assertEqual(report["decision"], "accept")

    def test_primary_critic_pass_does_not_publish_wrong_direction(self):
        optimistic = critic(
            difficulty="appropriate", distractors=1, knowledge=0,
        )
        invalid = spatial_verdict()
        with patch("tagging.review.LLMClient") as primary, patch(
            "tagging.quality_skill.arbiter.LLMClient"
        ) as secondary:
            primary.return_value.responses.create.return_value = ProviderResult(
                json.dumps(optimistic.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            secondary.return_value.responses.create.return_value = ProviderResult(
                json.dumps(invalid.model_dump(mode="json")),
                Completion.COMPLETE, "completed",
            )
            with self.assertRaises(QualityRegenerationError):
                check_tagged_quality(
                    URAL, language="Russian", category="Geography",
                    requested_difficulty=5, neighbors=[], mode=QualityMode.ENFORCE,
                    deadline=perf_counter() + 30, timeout_seconds=12,
                    shadow={"would_arbitrate": False},
                )
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(secondary.call_count, 1)
        self.assertEqual(URAL._review_report["decision"], "reject")
        self.assertEqual(
            URAL._review_report["details"]["arbitration"]["issues"],
            ["premise_invalid"],
        )


if __name__ == "__main__":
    unittest.main()
