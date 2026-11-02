"""Offline critic v5 regressions: no LLM calls, no DB, no network."""
import json
import unittest
from pathlib import Path

from quality_schema import IssueCode
from tagging.contract import TaggedQuestion
from tagging.review import TaggedReview, reconcile_tagged_review, tagged_assessment
from tagging.review_safeguards import find_safeguards


CASES = {
    entry["id"]: entry
    for entry in json.loads(
        (Path(__file__).parent / "fixtures" / "nov2_quality_regressions.json")
        .read_text(encoding="utf-8")
    )["cases"]
}


def candidate(case_id):
    return TaggedQuestion.model_validate(CASES[case_id]["question"])


def overly_optimistic_critic(question, *, distractors=0):
    """Realistic, coherent but WRONG all-pass model response from Nov 2."""
    keyed = question.correct_answer
    return TaggedReview.model_validate({
        "premise": "valid",
        "option_assessments": {
            letter: {
                "status": "yes" if letter == keyed else "no",
                "rationale": "independent check produced a definite choice",
            }
            for letter in "ABCD"
        },
        "facts": "pass", "options": "pass", "language": "pass",
        "leakage": "pass", "difficulty": "appropriate",
        "independent_answer": keyed, "issue_codes": [],
        "distractor_penalty": distractors, "knowledge_penalty": 0,
        "suitability_penalty": 0, "wording_penalty": 0,
        "rationale": "Model erroneously accepted this candidate.",
        "tags_valid": "pass", "fact_valid": "pass",
        "semantic_duplicate": False, "duplicate_match": "",
    })


class SourceBackedSafeguardTests(unittest.TestCase):
    def test_cannes_false_shared_winner_becomes_hard_fact_failure(self):
        q = candidate("cannes_1976_false_shared_winner")
        raw = overly_optimistic_critic(q)
        effective, evidence = reconcile_tagged_review(raw, q, requested_difficulty=11)
        self.assertEqual(evidence.codes, ["source_backed_false_premise"])
        self.assertEqual(effective.premise.value, "invalid")
        self.assertEqual(effective.facts.value, "fail")
        self.assertEqual(effective.fact_valid.value, "fail")
        self.assertIn(IssueCode.FACTUAL_ERROR, effective.issue_codes)
        self.assertEqual(tagged_assessment(effective, q.correct_answer)["critical"], "regenerate")
        self.assertEqual(raw.premise.value, "valid")  # original verdict intact

    def test_cannes_correct_year_and_unrelated_claims_not_blocked(self):
        q = candidate("cannes_1976_false_shared_winner")
        same_1975 = q.model_copy(update={"question": q.question.replace("1976", "1975")})
        self.assertFalse(find_safeguards(same_1975, requested_difficulty=11).false_premise)
        without_shared = q.model_copy(update={
            "question": "Какой фильм получил Золотую пальмовую ветвь в 1976 году? "
                        "И что вы знаете про фильм «Хроника лет огня»?"
        })
        self.assertFalse(find_safeguards(without_shared, requested_difficulty=11).false_premise)
        self.assertFalse(find_safeguards(q.model_copy(update={
            "question": "Какая картина получила награду в 1976 году?"
        }), requested_difficulty=11).false_premise)

    def test_transsib_weak_options_are_upgraded_without_fact_failure(self):
        q = candidate("transsib_obvious_western_terminus")
        raw = overly_optimistic_critic(q, distractors=1)
        effective, evidence = reconcile_tagged_review(raw, q, requested_difficulty=6)
        self.assertEqual(evidence.codes, ["source_backed_distractor_calibration"])
        self.assertEqual(raw.distractor_penalty, 1)
        self.assertGreaterEqual(effective.distractor_penalty, 2)
        self.assertEqual(effective.facts.value, "pass")
        self.assertEqual(effective.options.value, "pass")
        self.assertEqual(tagged_assessment(effective, q.correct_answer)["critical"], "none")

    def test_directional_calibration_is_narrow_and_does_not_invent_geo_knowledge(self):
        q = candidate("transsib_obvious_western_terminus")
        changed = q.model_copy(update={
            "answers": {**q.answers, "B": "Казань"}
        })
        self.assertEqual(find_safeguards(changed, requested_difficulty=6)
                         .minimum_distractor_penalty, 0)
        self.assertEqual(find_safeguards(q, requested_difficulty=5)
                         .minimum_distractor_penalty, 0)
        other = q.model_copy(update={
            "question": "Какой город является восточной конечной точкой Транссибирской магистрали?"
        })
        self.assertEqual(find_safeguards(other, requested_difficulty=6)
                         .minimum_distractor_penalty, 0)

    def test_xerox_brand_only_shortcut_overrides_model_leakage_pass(self):
        q = candidate("xerox_alto_brand_leak")
        raw = overly_optimistic_critic(q)
        effective, evidence = reconcile_tagged_review(raw, q, requested_difficulty=15)
        self.assertIn("literal_answer_leak", evidence.codes)
        self.assertEqual(effective.leakage.value, "fail")
        self.assertIn(IssueCode.ANSWER_LEAK, effective.issue_codes)
        self.assertEqual(tagged_assessment(effective, q.correct_answer)["critical"], "regenerate")
        self.assertEqual(raw.leakage.value, "pass")

    def test_brand_shortcut_requires_one_uniquely_named_answer(self):
        q = candidate("xerox_alto_brand_leak")
        q = q.model_copy(update={"answers": {**q.answers, "A": "Xerox Labs"}})
        self.assertFalse(find_safeguards(q, requested_difficulty=15).literal_answer_leak)

    def test_raw_option_evidence_multiple_correct_remains_diagnostic(self):
        from tagging.review import option_evidence_diagnostics

        q = candidate("transsib_obvious_western_terminus")
        raw = overly_optimistic_critic(q)
        raw_dict = raw.model_dump(mode="json")
        raw_dict["option_assessments"]["B"]["status"] = "yes"
        raw = TaggedReview.model_validate(raw_dict)
        effective, _ = reconcile_tagged_review(raw, q, requested_difficulty=6)
        self.assertEqual(effective.options.value, "pass")
        self.assertIn("nonunique_options_but_options_pass",
                      option_evidence_diagnostics(effective, q.correct_answer)["consistency_flags"])

    def test_uncertain_option_evidence_remains_visible_without_new_gate(self):
        from tagging.review import option_evidence_diagnostics

        q = candidate("transsib_obvious_western_terminus")
        raw = overly_optimistic_critic(q)
        raw_dict = raw.model_dump(mode="json")
        raw_dict["option_assessments"]["A"]["status"] = "uncertain"
        raw = TaggedReview.model_validate(raw_dict)
        effective, _ = reconcile_tagged_review(raw, q, requested_difficulty=6)
        self.assertEqual(effective.options.value, "pass")
        self.assertIn("uncertain_choices_but_options_pass",
                      option_evidence_diagnostics(effective, q.correct_answer)["consistency_flags"])


if __name__ == "__main__":
    unittest.main()
