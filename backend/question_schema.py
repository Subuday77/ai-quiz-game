"""Validate model-generated trivia before it enters a game session."""

from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Choice = Literal["A", "B", "C", "D"]


class QuizQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    difficulty: int = Field(ge=0, le=15)
    question: str = Field(min_length=5)
    answers: dict[Choice, str] = Field(min_length=4, max_length=4)
    correct_answer: Choice

    @field_validator("question", "answers")
    @classmethod
    def reject_control_characters(cls, value: str | dict[Choice, str]):
        """Reject all Unicode Cc controls before quality review / PostgreSQL.

        JSON Schema allows escaped NUL such as \\u0000. PostgreSQL text
        rejects it. Never guess or replace a damaged mathematical symbol.
        Tag/fact slugs are separately limited to canonical ASCII in TaggedQuestion.
        """
        strings = (value.values() if isinstance(value, dict) else (value,))
        for string in strings:
            if any(unicodedata.category(char) == "Cc" for char in string):
                raise ValueError("Question text and answers must not contain control characters")
        return value

    @model_validator(mode="after")
    def check_choices(self) -> "QuizQuestion":
        if set(self.answers) != {"A", "B", "C", "D"}:
            raise ValueError("Exactly A, B, C, D are required")
        normalized = [answer.strip().casefold() for answer in self.answers.values()]
        if any(not answer for answer in normalized):
            raise ValueError("Answer choices must not be empty")
        if len(set(normalized)) != 4:
            raise ValueError("Answer choices must be distinct")
        return self


class PublicQuestion(BaseModel):
    """The deliberately restricted browser response (no correct answer)."""

    question_id: str
    number: int
    difficulty: int
    question: str
    answers: dict[Choice, str]
    bonus: bool
    hidden_count: int = Field(default=0, ge=0, le=2)
    # Only the requesting player's own vote; no aggregate rating.
    my_vote: Literal[-1, 0, 1] = 0
