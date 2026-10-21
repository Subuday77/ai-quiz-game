"""Structured critic v2: independently report checks; Python owns publication policy.

The v1 review contract stays unchanged for compatibility.
"""
from copy import deepcopy
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_schema import IssueCode, ReviewDecision


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


class DifficultyRating(str, Enum):
    APPROPRIATE = "appropriate"
    TOO_EASY = "too_easy"
    TOO_HARD = "too_hard"
    UNCERTAIN = "uncertain"


class QualityReviewV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: CheckStatus
    options: CheckStatus
    language: CheckStatus
    distractors: CheckStatus
    leakage: CheckStatus
    difficulty: DifficultyRating
    independent_answer: Literal["A", "B", "C", "D", "unknown"]
    issue_codes: list[IssueCode] = Field(max_length=8)
    rationale: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def validate_report(self):
        if not self.rationale.strip():
            raise ValueError("Blank review rationale")
        if len(set(self.issue_codes)) != len(self.issue_codes):
            raise ValueError("Duplicate issue codes")
        return self


# This mapping deliberately excludes difficulty: difficulty alone is advisory.
FACTUAL_BLOCKERS = frozenset({
    IssueCode.FACTUAL_ERROR,
    IssueCode.MULTIPLE_CORRECT,
    IssueCode.AMBIGUOUS,
    IssueCode.ANSWER_LEAK,
    IssueCode.LANGUAGE_PROBLEM,
    IssueCode.OTHER,
})


def decide_review(report: QualityReviewV2, supplied_answer: str) -> tuple[ReviewDecision, list[str]]:
    """Conservative deterministic policy, independent of the model's desired verdict."""
    issues = [code.value for code in report.issue_codes]
    statuses = (report.facts, report.options, report.language,
                report.distractors, report.leakage)

    if any(status == CheckStatus.FAIL for status in statuses) or any(
        code in FACTUAL_BLOCKERS for code in report.issue_codes
    ):
        return ReviewDecision.REGENERATE, issues or [IssueCode.OTHER.value]

    if (any(status == CheckStatus.UNCERTAIN for status in statuses)
            or IssueCode.NEEDS_VERIFICATION in report.issue_codes
            or report.independent_answer == "unknown"
            or report.independent_answer != supplied_answer):
        return ReviewDecision.ESCALATE, issues or [IssueCode.NEEDS_VERIFICATION.value]

    # Difficulty is observed and logged but does not block publication by itself.
    difficulty_only = [code for code in issues if code != IssueCode.DIFFICULTY_MISMATCH.value]
    if difficulty_only:
        return ReviewDecision.ESCALATE, difficulty_only
    return ReviewDecision.ACCEPT, issues


QUALITY_V2_SCHEMA_NAME = "quality_review_v2"
_V2_SCHEMA = {
    "type": "object",
    "properties": {
        **{field: {"type": "string", "enum": [status.value for status in CheckStatus]}
           for field in ("facts", "options", "language", "distractors", "leakage")},
        "difficulty": {"type": "string", "enum": [level.value for level in DifficultyRating]},
        "independent_answer": {"type": "string", "enum": ["A", "B", "C", "D", "unknown"]},
        "issue_codes": {"type": "array", "items": {"type": "string", "enum": [x.value for x in IssueCode]}},
        "rationale": {"type": "string"},
    },
    "required": ["facts", "options", "language", "distractors", "leakage",
                 "difficulty", "independent_answer", "issue_codes", "rationale"],
    "additionalProperties": False,
}


def quality_review_v2_schema() -> dict:
    return deepcopy(_V2_SCHEMA)
