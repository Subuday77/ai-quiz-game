"""Pure quiz rules: no terminal I/O, database or AI provider dependencies."""

from dataclasses import dataclass
from typing import Literal

MAIN_QUESTION_COUNT = 15
BONUS_DIFFICULTY = 15  # Main questions use difficulty levels 0 through 14.
STAR_MILESTONES = {5: "bronze", 10: "silver", 15: "gold"}

GamePhase = Literal["main", "bonus", "completed", "lost"]


@dataclass(frozen=True)
class AnswerResult:
    """Public result of a checked answer; no correct-answer key is exposed."""

    correct: bool
    phase: GamePhase
    achievement: str | None = None


@dataclass
class GameState:
    """Track progression and eligibility for the optional bonus question."""

    correct_main_answers: int = 0
    hints_used: int = 0
    phase: GamePhase = "main"
    bonus_correct: bool | None = None

    @property
    def current_difficulty(self) -> int:
        """Return the next question's difficulty, or reject a finished game."""
        if self.phase == "main":
            return self.correct_main_answers
        if self.phase == "bonus":
            return BONUS_DIFFICULTY
        raise RuntimeError("The game has already ended")

    def record_hint_use(self) -> None:
        """Any gameplay assistance disqualifies the secret bonus question."""
        if self.phase != "main":
            raise RuntimeError("Hints are only available during the main round")
        self.hints_used += 1

    def submit_answer(self, is_correct: bool) -> AnswerResult:
        """Move the game forward after the caller has verified an answer."""
        if self.phase not in ("main", "bonus"):
            raise RuntimeError("Cannot answer after the game has ended")

        if self.phase == "bonus":
            self.bonus_correct = is_correct
            self.phase = "completed"
            return AnswerResult(
                correct=is_correct,
                phase=self.phase,
                achievement="platinum" if is_correct else None,
            )

        if not is_correct:
            self.phase = "lost"
            return AnswerResult(correct=False, phase=self.phase)

        self.correct_main_answers += 1
        achievement = STAR_MILESTONES.get(self.correct_main_answers)
        if self.correct_main_answers == MAIN_QUESTION_COUNT:
            self.phase = "bonus" if self.hints_used == 0 else "completed"

        return AnswerResult(
            correct=True,
            phase=self.phase,
            achievement=achievement,
        )
