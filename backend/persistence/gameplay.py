"""Transactional persistence for quiz rounds.

This layer does not call an LLM or construct HTTP responses. Only validated
QuizQuestion instances may be passed to offer_question; generation/quality
checks are the responsibility of the caller. A running game can be reloaded
from PostgreSQL without a Python process-local sessions dictionary.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from game_logic import AnswerResult, GameState
from persistence.models import GameRun, Question, QuestionAttempt, QuestionReview, StarAward, GoldStarMovement, Topic, User
from persistence.seed_topics import category_slug
from question_schema import Choice, QuizQuestion


class GameNotFound(LookupError):
    """The round is absent, or it is not owned by the requester."""


class GameConflict(RuntimeError):
    """The requested state transition is invalid."""


class TopicsNotSeeded(RuntimeError):
    """A known category is missing from the PostgreSQL topic catalog."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_game(db: Session, *, user_id: UUID, language: str) -> GameRun:
    """Commit a new player-owned round. Authentication is done by the caller."""
    run = GameRun(user_id=user_id, language=language, status="main")
    db.add(run)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(run)
    return run


def owned_game(db: Session, *, run_id: UUID, user_id: UUID,
               for_update: bool = False) -> GameRun:
    query = select(GameRun).where(GameRun.id == run_id, GameRun.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    run = db.scalar(query)
    if run is None:
        raise GameNotFound("Game not found")
    return run


def game_state(run: GameRun) -> GameState:
    """Rebuild pure game progression from persisted columns."""
    return GameState(
        correct_main_answers=run.correct_main_answers,
        hints_used=run.hints_used,
        phase=run.status,
        bonus_correct=run.bonus_correct,
    )


def pending_question(db: Session, *, run_id: UUID) -> tuple[QuestionAttempt, Question] | None:
    attempt = db.scalar(
        select(QuestionAttempt)
        .where(QuestionAttempt.run_id == run_id,
               QuestionAttempt.answered_at.is_(None))
        .order_by(QuestionAttempt.number.desc())
    )
    if attempt is None:
        return None
    question = db.get(Question, attempt.question_id)
    if question is None:
        raise GameConflict("Question missing from persisted round")
    return attempt, question


def _fingerprint(question: QuizQuestion, category: str) -> str:
    # Same wording at another difficulty/topic is a separate immutable candidate.
    payload = {"question": question.question.strip(), "answers": question.answers,
               "difficulty": question.difficulty, "category": category}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def offer_question(
    db: Session, *, run_id: UUID, user_id: UUID,
    question: QuizQuestion, category: str,
    provider: str, model: str,
    prompt_version: str = "v1", schema_version: str = "question_v1",
    dedup_enforce: bool = False,
    expected_bonus_topic_index: int | None = None,
) -> tuple[QuestionAttempt, Question]:
    """Store one already-validated candidate, or return the cached pending one.

    The row lock serializes concurrent requests for the same run on PostgreSQL.
    Model calls should happen OUTSIDE this transaction, before invoking this
    function, so slow generation does not hold the game row lock.
    """
    try:
        if dedup_enforce:
            from tagging.contract import TaggedQuestion
            from tagging.core import require_unique
            from tagging.repository import history_for_user
            if not isinstance(question, TaggedQuestion):
                raise GameConflict("Tagged workflow requires verified fact metadata")
            # Serialize ALL question commits for this player across game runs.
            db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game has ended")
        # A topic switch may happen while the old AI request is in flight.
        # Discard its late result rather than present a wrong-category bonus.
        if (run.status == "bonus" and expected_bonus_topic_index is not None
                and run.bonus_topic_index != expected_bonus_topic_index):
            raise GameConflict("Bonus topic changed during generation")
        cached = pending_question(db, run_id=run.id)
        if cached:
            db.commit()
            return cached
        if dedup_enforce:
            # Atomic recheck after another parallel request may have committed.
            require_unique(question, history_for_user(db, user_id))
        expected_difficulty = game_state(run).current_difficulty
        if question.difficulty != expected_difficulty:
            raise GameConflict("Wrong question difficulty for current game state")
        topic = db.scalar(select(Topic).where(Topic.slug == category_slug(category)))
        if topic is None:
            raise TopicsNotSeeded("Run python -m persistence.seed_topics first")
        fingerprint = _fingerprint(question, category)
        lookup = select(Question).where(
            Question.language == run.language,
            Question.fingerprint == fingerprint,
        )
        stored = db.scalar(lookup)
        if stored is None:
            candidate = Question(
                topic_id=topic.id, language=run.language,
                requested_difficulty=question.difficulty,
                text=question.question, choices=question.answers,
                correct_key=question.correct_answer, fingerprint=fingerprint,
                status="approved", generator_provider=provider,
                generator_model=model, prompt_version=prompt_version,
                schema_version=schema_version,
            )
            try:
                # Across games, two AI calls can produce the same candidate.
                # A savepoint contains the global unique-index race without
                # aborting the transaction holding this round's row lock.
                with db.begin_nested():
                    db.add(candidate)
                    db.flush()
                stored = candidate
            except IntegrityError:
                stored = db.scalar(lookup)
                if stored is None:
                    raise
        if (stored.correct_key != question.correct_answer
                or stored.status != "approved"
                or stored.topic_id != topic.id
                or stored.requested_difficulty != question.difficulty):
            raise GameConflict("Existing question is conflicting or not approved")
        if dedup_enforce:
            from tagging.repository import save_metadata
            save_metadata(db, stored, question)
            # The reviewer runs BEFORE the DB transaction; persist its full
            # accepted verdict alongside the question, tags and attempt.
            # PrivateAttr remains absent from all player-facing JSON.
            review = question._review_report
            if review is not None:
                db.add(QuestionReview(question_id=stored.id, **review))
        next_number = run.correct_main_answers + 1
        attempt = QuestionAttempt(
            run_id=run.id, question_id=stored.id,
            number=next_number,
            requested_difficulty=expected_difficulty,
        )
        db.add(attempt)
        if run.status == "bonus":
            run.bonus_topic_change_unlocked = False
        db.commit()
        db.refresh(attempt)
        return attempt, stored
    except Exception:
        db.rollback()
        raise


def record_answer(
    db: Session, *, run_id: UUID, user_id: UUID,
    question_id: UUID, selected: Choice,
) -> tuple[AnswerResult, str]:
    """Accept a single answer, commit progress and star award atomically.

    No wallet credits or arbitrary score formula are invented here.
    A replay fails before any mutation. Locking works across web workers on
    PostgreSQL and the unique star constraint is a second safety net.
    """
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game has ended")
        cached = pending_question(db, run_id=run.id)
        if cached is None:
            raise GameConflict("No matching unanswered question")
        attempt, question = cached
        if question.id != question_id:
            raise GameConflict("No matching unanswered question")

        now = _now()
        was_bonus = run.status == "bonus"
        state = game_state(run)
        is_correct = selected == question.correct_key
        result = state.submit_answer(is_correct)

        attempt.answered_at = now
        attempt.selected_key = selected
        attempt.is_correct = is_correct
        attempt.answer_duration_ms = max(
            0, int((now - _aware(attempt.shown_at)).total_seconds() * 1000)
        )
        run.correct_main_answers = state.correct_main_answers
        run.hints_used = state.hints_used
        run.status = state.phase
        run.bonus_correct = state.bonus_correct
        if state.phase in ("completed", "lost"):
            run.finished_at = now
        if result.achievement:
            db.add(StarAward(
                user_id=user_id, run_id=run.id, tier=result.achievement
            ))
        if was_bonus and not is_correct:
            # Earned gold stays in the audit history but is forfeited as risk.
            db.add(GoldStarMovement(
                user_id=user_id, run_id=run.id,
                amount=-1, reason="bonus_lost",
            ))
        # Unused helper cost is returned on finishing Q15, irrespective
        # of whether the player later wins or loses the bonus.
        if run.correct_main_answers == 15:
            from platinum_helper.service import refund_if_earned
            refund_if_earned(db, run)
        db.commit()
        return result, question.correct_key
    except Exception:
        db.rollback()
        raise


def decline_bonus(db: Session, *, run_id: UUID, user_id: UUID) -> GameRun:
    """Keep the Q15 gold without risking it; do not issue Q16."""
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status != "bonus" or run.correct_main_answers != 15:
            raise GameConflict("Bonus decision is not available")
        if pending_question(db, run_id=run_id) is not None:
            raise GameConflict("Cannot decline an already issued bonus question")
        run.status = "completed"
        run.finished_at = _now()
        db.commit()
        return run
    except Exception:
        db.rollback()
        raise
