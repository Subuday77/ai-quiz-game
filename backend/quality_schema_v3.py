"""Audit-only v3 contract: critical checks plus graded editorial penalties.

The model reports severity by category. Python owns scoring and any future threshold.
The v1 and v2 contracts remain untouched.
"""
from copy import deepcopy
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality_schema import IssueCode
from quality_schema_v2 import CheckStatus, DifficultyRating


class QualityReviewV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: CheckStatus
    options: CheckStatus
    language: CheckStatus
    leakage: CheckStatus
    difficulty: DifficultyRating
    independent_answer: Literal["A", "B", "C", "D", "unknown"]
    issue_codes: list[IssueCode] = Field(max_length=8)

    # Independent, noncritical grades; a zero means no demonstrated problem.
    distractor_penalty: int = Field(ge=0, le=3)
    knowledge_penalty: int = Field(ge=0, le=2)
    suitability_penalty: int = Field(ge=0, le=2)
    wording_penalty: int = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def validate_report(self):
        if not self.rationale.strip():
            raise ValueError("Blank rationale")
        if len(set(self.issue_codes)) != len(self.issue_codes):
            raise ValueError("Duplicate issue codes")
        return self


CRITICAL_CODES = frozenset({
    IssueCode.FACTUAL_ERROR,
    IssueCode.MULTIPLE_CORRECT,
    IssueCode.AMBIGUOUS,
    IssueCode.ANSWER_LEAK,
    IssueCode.LANGUAGE_PROBLEM,
})


def audit_score(report: QualityReviewV3, supplied_answer: str) -> dict:
    """Compute score and critical recommendation; never apply a threshold."""
    penalties = {
        "distractors": report.distractor_penalty,
        "knowledge": report.knowledge_penalty,
        "suitability": report.suitability_penalty,
        "wording": report.wording_penalty,
    }
    total = sum(penalties.values())
    if (any(getattr(report, name) == CheckStatus.FAIL
            for name in ("facts", "options", "language", "leakage"))
            or any(code in CRITICAL_CODES for code in report.issue_codes)):
        critical = "regenerate"
    elif (any(getattr(report, name) == CheckStatus.UNCERTAIN
              for name in ("facts", "options", "language", "leakage"))
          or report.independent_answer == "unknown"
          or report.independent_answer != supplied_answer
          or IssueCode.NEEDS_VERIFICATION in report.issue_codes
          or IssueCode.OTHER in report.issue_codes):
        critical = "escalate"
    else:
        critical = "none"
    return {
        "total": total,
        "penalties": penalties,
        "critical": critical,
        # Diagnostic what-if thresholds only. No score-based rejection in v3.
        "threshold_preview": {str(t): total >= t for t in (2, 3, 4, 5)},
    }


QUALITY_V3_SCHEMA_NAME = "quality_review_v3"
_V3_SCHEMA = {
    "type": "object",
    "properties": {
        **{name: {"type": "string", "enum": [x.value for x in CheckStatus]}
           for name in ("facts", "options", "language", "leakage")},
        "difficulty": {"type": "string", "enum": [x.value for x in DifficultyRating]},
        "independent_answer": {"type": "string", "enum": ["A", "B", "C", "D", "unknown"]},
        "issue_codes": {"type": "array", "items": {
            "type": "string", "enum": [x.value for x in IssueCode]}},
        "distractor_penalty": {"type": "integer", "enum": [0, 1, 2, 3]},
        "knowledge_penalty": {"type": "integer", "enum": [0, 1, 2]},
        "suitability_penalty": {"type": "integer", "enum": [0, 1, 2]},
        "wording_penalty": {"type": "integer", "enum": [0, 1]},
        "rationale": {"type": "string"},
    },
    "required": [
        "facts", "options", "language", "leakage", "difficulty",
        "independent_answer", "issue_codes", "distractor_penalty",
        "knowledge_penalty", "suitability_penalty", "wording_penalty", "rationale",
    ],
    "additionalProperties": False,
}


def quality_review_v3_schema() -> dict:
    return deepcopy(_V3_SCHEMA)
