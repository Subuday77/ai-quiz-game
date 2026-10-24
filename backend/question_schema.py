"""Validate model-generated trivia before it enters a game session."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Choice = Literal["A", "B", "C", "D"]


class QuizQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    difficulty: int = Field(ge=0, le=15)
    question: str = Field(min_length=5)
    answers: dict[Choice, str] = Field(min_length=4, max_length=4)
    correct_answer: Choice

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
