"""Typed internal reviewer verdict and a stable portable transport schema."""

from copy import deepcopy
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QualityMode(str, Enum):
    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"


class ReviewDecision(str, Enum):
    ACCEPT = "accept"
    REGENERATE = "regenerate"
    ESCALATE = "escalate"


class IssueCode(str, Enum):
    FACTUAL_ERROR = "factual_error"
    MULTIPLE_CORRECT = "multiple_correct"
    AMBIGUOUS = "ambiguous"
    ANSWER_LEAK = "answer_leak"
    DIFFICULTY_MISMATCH = "difficulty_mismatch"
    LANGUAGE_PROBLEM = "language_problem"
    NEEDS_VERIFICATION = "needs_verification"
    OTHER = "other"


class QualityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    issue_codes: list[IssueCode] = Field(max_length=8)
    independent_answer: Literal["A", "B", "C", "D", "unknown"]
    rationale: str = Field(min_length=1, max_length=600)

    @field_validator("rationale")
    @classmethod
    def nonempty_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Review rationale must not be blank")
        return value

    @model_validator(mode="after")
    def consistent_verdict(self):
        if len(set(self.issue_codes)) != len(self.issue_codes):
            raise ValueError("Duplicate review issue codes")
        if self.decision == ReviewDecision.ACCEPT:
            if self.issue_codes or self.independent_answer == "unknown":
                raise ValueError("Accept requires a known independent answer and no issues")
        elif not self.issue_codes:
            raise ValueError("A rejected or uncertain review requires issue codes")
        return self


QUALITY_SCHEMA_NAME = "quality_review_v1"
_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": [d.value for d in ReviewDecision]},
        "issue_codes": {"type": "array", "items": {
            "type": "string", "enum": [code.value for code in IssueCode]}},
        "independent_answer": {"type": "string", "enum": ["A", "B", "C", "D", "unknown"]},
        "rationale": {"type": "string"},
    },
    "required": ["decision", "issue_codes", "independent_answer", "rationale"],
    "additionalProperties": False,
}


def quality_review_schema() -> dict:
    return deepcopy(_SCHEMA)
