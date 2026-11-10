"""Experimental two-stage critic: descriptive option analysis -> deterministic score.

This module is used only by the standalone calibration runner.
The live quality contracts v1/v2/v3 and game behavior stay unchanged.
"""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_schema import IssueCode
from quality_schema_v2 import CheckStatus, DifficultyRating
from quality_schema_v3 import QualityReviewV3, audit_score


class WrongOptionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["A", "B", "C", "D"]
    plausibility: Literal["obvious_wrong", "plausible_wrong", "near_miss"]
    separation_knowledge: Literal["basic", "general", "specialist"]
    reason: str = Field(min_length=1, max_length=180)


class TwoStageReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: CheckStatus
    options: CheckStatus
    language: CheckStatus
    leakage: CheckStatus
    difficulty: DifficultyRating
    independent_answer: Literal["A", "B", "C", "D", "unknown"]
    issue_codes: list[IssueCode] = Field(max_length=8)
    wrong_options: list[WrongOptionEvidence] = Field(min_length=3, max_length=3)
    iconic_key_association: bool
    knowledge_penalty: int = Field(ge=0, le=2)
    suitability_penalty: int = Field(ge=0, le=2)
    wording_penalty: int = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def check_unique_choices(self):
        choices = [item.choice for item in self.wrong_options]
        if len(set(choices)) != 3:
            raise ValueError("Each wrong choice must be analyzed exactly once")
        if len(set(self.issue_codes)) != len(self.issue_codes):
            raise ValueError("Duplicate issue codes")
        return self


def distractor_penalty(review: TwoStageReview, requested_difficulty: int,
                       correct_answer: str) -> int:
    """Deterministic level-aware penalty: no LLM-supplied numeric rating.

    Three kinds of evidence: overly fine differentiation at easy levels,
    throwaway distractors at higher levels, and a famous association that
    defeats otherwise merely topical alternatives. Only the strongest
    applicable problem is counted, to avoid double penalties.
    """
    if not 0 <= requested_difficulty <= 15:
        raise ValueError("Difficulty outside game scale")
    wrong = review.wrong_options
    if set(item.choice for item in wrong) != set("ABCD") - {correct_answer}:
        raise ValueError("Evidence must cover precisely the three wrong options")

    hard_near = sum(
        item.plausibility == "near_miss" and
        item.separation_knowledge == "specialist" for item in wrong
    )
    general_near = sum(
        item.plausibility == "near_miss" and
        item.separation_knowledge == "general" for item in wrong
    )
    throwaways = sum(item.plausibility == "obvious_wrong" for item in wrong)
    strong_competitor = any(
        item.plausibility == "near_miss" and
        item.separation_knowledge in {"general", "specialist"} for item in wrong
    )

    if requested_difficulty <= 3:
        if hard_near >= 2:
            return 3
        if hard_near >= 1:
            return 2
        if requested_difficulty <= 1 and general_near:
            return 1
        return 0

    if requested_difficulty <= 6:
        score = 2 if throwaways >= 2 else (1 if throwaways == 1 else 0)
        if review.iconic_key_association and not strong_competitor and requested_difficulty >= 5:
            score = max(score, 2)
        return score

    score = 3 if throwaways == 3 else (2 if throwaways == 2 else (1 if throwaways == 1 else 0))
    if review.iconic_key_association:
        # One credible near-miss resists the famous-answer shortcut.
        score = max(score, 1 if strong_competitor else 2)
    return score


def score_two_stage(review: TwoStageReview, difficulty: int, correct_answer: str) -> dict:
    points = distractor_penalty(review, difficulty, correct_answer)
    converted = QualityReviewV3.model_validate({
        "facts": review.facts,
        "options": review.options,
        "language": review.language,
        "leakage": review.leakage,
        "difficulty": review.difficulty,
        "independent_answer": review.independent_answer,
        "issue_codes": review.issue_codes,
        "distractor_penalty": points,
        "knowledge_penalty": review.knowledge_penalty,
        "suitability_penalty": review.suitability_penalty,
        "wording_penalty": review.wording_penalty,
        "rationale": review.rationale,
    })
    return audit_score(converted, correct_answer)


SCHEMA_NAME = "quality_review_two_stage"
_SCHEMA = {
    "type": "object",
    "properties": {
        **{name: {"type": "string", "enum": [s.value for s in CheckStatus]}
           for name in ("facts", "options", "language", "leakage")},
        "difficulty": {"type": "string", "enum": [s.value for s in DifficultyRating]},
        "independent_answer": {"type": "string", "enum": ["A", "B", "C", "D", "unknown"]},
        "issue_codes": {"type": "array", "items": {"type": "string",
                        "enum": [s.value for s in IssueCode]}},
        "wrong_options": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {
                          "choice": {"type": "string", "enum": ["A", "B", "C", "D"]},
                          "plausibility": {"type": "string", "enum": [
                              "obvious_wrong", "plausible_wrong", "near_miss"]},
                          "separation_knowledge": {"type": "string", "enum": [
                              "basic", "general", "specialist"]},
                          "reason": {"type": "string"},
                      },
                      "required": ["choice", "plausibility",
                                   "separation_knowledge", "reason"],
                      "additionalProperties": False},
        },
        "iconic_key_association": {"type": "boolean"},
        "knowledge_penalty": {"type": "integer", "enum": [0, 1, 2]},
        "suitability_penalty": {"type": "integer", "enum": [0, 1, 2]},
        "wording_penalty": {"type": "integer", "enum": [0, 1]},
        "rationale": {"type": "string"},
    },
    "required": [
        "facts", "options", "language", "leakage", "difficulty",
        "independent_answer", "issue_codes", "wrong_options",
        "iconic_key_association", "knowledge_penalty", "suitability_penalty",
        "wording_penalty", "rationale",
    ],
    "additionalProperties": False,
}


def two_stage_schema() -> dict:
    return deepcopy(_SCHEMA)
