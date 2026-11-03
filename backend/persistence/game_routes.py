"""Authenticated, PostgreSQL-backed v2 game endpoints.

The legacy prototype keeps /api/games until the Angular UI is switched.
These endpoints use request-scoped SQLAlchemy sessions and row-level locking
for irreversible answer transitions.
"""
import os
import random
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from consts import LANGUAGE_DICT, QUESTION_CATEGORIES
from diagnostics import event
from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.gameplay import (
    GameConflict, GameNotFound, TopicsNotSeeded, create_game, game_state,
    offer_question, owned_game, pending_question, record_answer,
)
from persistence.models import Topic, User
from persistence.seed_topics import category_slug
from question_schema import Choice, PublicQuestion

router = APIRouter(prefix="/api/v2/games", tags=["Games — PostgreSQL"])


class NewGame(BaseModel):
    language: str = "English"


class AnswerInput(BaseModel):
    question_id: UUID
    answer: Choice


class GameInfo(BaseModel):
    game_id: UUID
    language: str
    phase: str
    correct_main_answers: int


class BonusTopicResponse(BaseModel):
    category: str
    cycle_reset: bool
    topic_index: int


class AnswerFeedback(BaseModel):
    correct: bool
    correct_answer: Choice
    phase: str
    correct_main_answers: int
    achievement: str | None = None


def _lookup(db: Session, run_id: UUID, user_id: UUID):
    try:
        return owned_game(db, run_id=run_id, user_id=user_id)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None


def _public(attempt, stored, phase: str) -> PublicQuestion:
    return PublicQuestion(
        question_id=str(attempt.question_id),
        number=attempt.number,
        difficulty=attempt.requested_difficulty,
        question=stored.text,
        answers=stored.choices,
        bonus=phase == "bonus",
    )


def _category_for_round(run_id: UUID, number: int,
                        bonus_topic_index: int = 0) -> str:
    """Stable per-round shuffled category order, surviving process restart."""
    if not 1 <= number <= 16:
        raise GameConflict("Invalid round question number")
    categories = random.Random(run_id.int).sample(
        QUESTION_CATEGORIES, k=len(QUESTION_CATEGORIES)
    )
    if number == 16:
        return categories[(15 + bonus_topic_index) % len(categories)]
    return categories[number - 1]



def _unlock_bonus_topic_change(db: Session, *, run_id: UUID, user_id: UUID) -> None:
    """Persist permission after actual generation failure, not after success."""
    db.rollback()
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status == "bonus" and pending_question(db, run_id=run.id) is None:
            run.bonus_topic_change_unlocked = True
            db.commit()
            event("bonus_topic_change_unlocked", topic_index=run.bonus_topic_index)
        else:
            db.rollback()
    except Exception as exc:
        db.rollback()
        event("bonus_topic_change_unlock_failed", error_type=type(exc).__name__)


@router.post("/{run_id}/bonus/change-topic", response_model=BonusTopicResponse)
def change_bonus_topic(run_id: UUID, request: Request,
                       user: User = Depends(authenticated_user),
                       db: Session = Depends(get_db)):
    """Change Q16 topic after a failed generation, indefinitely and cyclically."""
    validate_origin(request)
    try:
        run = owned_game(db, run_id=run_id, user_id=user.id, for_update=True)
        if (run.status != "bonus" or run.correct_main_answers != 15
                or pending_question(db, run_id=run.id) is not None
                or not run.bonus_topic_change_unlocked):
            db.rollback()
            raise HTTPException(status_code=409,
                                detail="Bonus topic cannot be changed now")
        run.bonus_topic_index = (
            run.bonus_topic_index + 1
        ) % len(QUESTION_CATEGORIES)
        index = run.bonus_topic_index
        db.commit()
    except HTTPException:
        raise
    except GameNotFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="Game not found") from None
    except Exception:
        db.rollback()
        raise
    category = _category_for_round(run_id, 16, index)
    event("bonus_topic_changed", topic_index=index, cycle_reset=(index == 0))
    return BonusTopicResponse(category=category,
                              cycle_reset=index == 0, topic_index=index)


def _generation_metadata() -> tuple[str, str]:
    provider = (os.environ.get("LLM_PROVIDER") or "unknown").strip().lower()[:40]
    if provider not in ("openai", "nvidia", "bedrock"):
        return "unknown", "unknown"
    model = (os.environ.get(f"{provider.upper()}_MODEL_ID") or "unknown").strip()[:130]
    return provider, model or "unknown"


@router.post("", response_model=GameInfo, status_code=201)
def new_game(payload: NewGame, request: Request,
             user: User = Depends(authenticated_user),
             db: Session = Depends(get_db)):
    validate_origin(request)
    if payload.language not in LANGUAGE_DICT.values():
        raise HTTPException(status_code=422, detail="Unsupported language")
    existing = set(db.scalars(select(Topic.slug)))
    if not {category_slug(name) for name in QUESTION_CATEGORIES}.issubset(existing):
        raise HTTPException(status_code=503, detail="Topic catalog not initialized")
    run = create_game(db, user_id=user.id, language=payload.language)
    return GameInfo(
        game_id=run.id, language=run.language, phase=run.status,
        correct_main_answers=run.correct_main_answers,
    )


@router.get("/{run_id}", response_model=GameInfo)
def game_info(run_id: UUID, user: User = Depends(authenticated_user),
              db: Session = Depends(get_db)):
    run = _lookup(db, run_id, user.id)
    return GameInfo(
        game_id=run.id, language=run.language, phase=run.status,
        correct_main_answers=run.correct_main_answers,
    )


@router.get("/{run_id}/question", response_model=PublicQuestion)
def get_question(run_id: UUID, user: User = Depends(authenticated_user),
                 db: Session = Depends(get_db)):
    run = _lookup(db, run_id, user.id)
    if run.status not in ("main", "bonus"):
        raise HTTPException(status_code=409, detail="Game has ended")

    cached = pending_question(db, run_id=run.id)
    if cached is not None:
        event("question_cache", hit=True)
        return _public(*cached, phase=run.status)

    event("question_cache", hit=False)
    difficulty = game_state(run).current_difficulty
    bonus_index = run.bonus_topic_index if run.status == "bonus" else None
    category = _category_for_round(
        run.id, run.correct_main_answers + 1, bonus_index or 0
    )
    language = run.language
    if db.scalar(select(Topic.id).where(
            Topic.slug == category_slug(category))) is None:
        raise HTTPException(status_code=503, detail="Topic catalog not initialized")

    # Release read transaction/connection before potentially slow AI generation.
    # A second request can make progress; the later write lock will re-check
    # state and reuse a concurrently stored pending question.
    db.rollback()

    # Deferred import avoids coupling the persistence layer to AI provider
    # SDKs and reuses the same retry/quality/leak policy as legacy games.
    from api import generate_valid_question

    # Explicit opt-in: old question transport and all existing API tests
    # stay unchanged until the history backfill is complete and verified.
    tagged = os.getenv("AI_TAG_DEDUP_ENABLED", "false").strip().lower() == "true"
    if tagged:
        from tagging.core import HistoryNotTagged, generator_context
        from tagging.repository import history_for_user
        try:
            history = history_for_user(db, user.id)
        except HistoryNotTagged:
            raise HTTPException(
                status_code=503,
                detail="Historical questions lack verified tags; run tagging.backfill",
            ) from None
        exclusions = generator_context(history, category_slug(category))
        db.rollback()  # No database connection held during slow AI calls.
        # A rejected-but-factually-clean, too-easy-only question can be
        # retained in memory. A globally approved pooled question always wins.
        too_easy_reserve = []
        try:
            candidate = generate_valid_question(
                difficulty, language, category,
                tag_history=history, tag_context=exclusions,
                too_easy_reserve=too_easy_reserve,
            )
        except HTTPException as generation_error:
            recoverable = (generation_error.status_code in (502, 503)
                           or (generation_error.status_code == 409
                               and generation_error.detail == {"code": "duplicate_exhausted"}))
            if not recoverable:
                _unlock_bonus_topic_change(db, run_id=run_id, user_id=user.id)
                raise
            from persistence.question_pool import (
                enabled as fallback_enabled, offer_approved_pool_question, scan_limit,
            )
            from tagging.review import is_strict_too_easy_reserve
            try:
                use_pool = fallback_enabled("QUIZ_QUESTION_POOL_FALLBACK_ENABLED")
                use_reserve = (
                    run.status != "bonus"
                    and fallback_enabled("QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED")
                )
                limit = scan_limit() if use_pool else 1
            except ValueError:
                event("question_fallback_configuration_failed")
                _unlock_bonus_topic_change(db, run_id=run_id, user_id=user.id)
                raise HTTPException(
                    status_code=503, detail="Invalid question fallback configuration"
                ) from None
            if use_pool:
                try:
                    pooled = offer_approved_pool_question(
                        db, run_id=run_id, user_id=user.id,
                        category=category, max_candidates=limit,
                    )
                except Exception as exc:
                    # A broken optional fallback must not accidentally bypass
                    # the normal quality gates or expose a private answer.
                    event("question_pool_fallback_failed", error_type=type(exc).__name__)
                    db.rollback()
                else:
                    if pooled is not None:
                        return _public(
                            *pooled, phase="bonus" if pooled[0].number == 16 else "main",
                        )
            if not use_reserve or not too_easy_reserve:
                _unlock_bonus_topic_change(db, run_id=run_id, user_id=user.id)
                raise
            candidate = next(
                (item for item in too_easy_reserve
                 if is_strict_too_easy_reserve(item)), None,
            )
            if candidate is None:
                _unlock_bonus_topic_change(db, run_id=run_id, user_id=user.id)
                raise
            # Keep the original rejected verdict for audit. This emergency
            # publication is NEVER eligible for the normal approved pool,
            # which requires a prior critic decision='accept'.
            report = candidate._review_report
            report["details"]["fallback"] = {
                "reason": "too_easy_only",
                "original_decision": report["decision"],
                "generation_status": generation_error.status_code,
            }
            report["decision"] = "fallback_too_easy"
            event("question_too_easy_fallback", outcome="selected",
                  requested_difficulty=difficulty)
    else:
        try:
            candidate = generate_valid_question(difficulty, language, category)
        except HTTPException:
            _unlock_bonus_topic_change(db, run_id=run_id, user_id=user.id)
            raise
    provider, model = _generation_metadata()
    try:
        attempt, stored = offer_question(
            db, run_id=run_id, user_id=user.id, question=candidate,
            category=category, provider=provider, model=model,
            prompt_version="tagged-v2" if tagged else "v1",
            schema_version="question_tagged_v2" if tagged else "question_v1",
            dedup_enforce=tagged,
            expected_bonus_topic_index=bonus_index,
        )
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict:
        raise HTTPException(status_code=409, detail="Game state changed; reload question") from None
    except TopicsNotSeeded:
        raise HTTPException(status_code=503, detail="Topic catalog not initialized") from None
    except Exception as exc:
        from tagging.core import DuplicateQuestionError, HistoryNotTagged
        if isinstance(exc, DuplicateQuestionError):
            raise HTTPException(status_code=409, detail="Duplicate question rejected; retry") from None
        if isinstance(exc, HistoryNotTagged):
            raise HTTPException(status_code=503, detail="Historical tag backfill required") from None
        raise

    # The previous round may have advanced while a concurrent AI call ran.
    # The commit-side row lock guarantees we only return the active question.
    return _public(attempt, stored, phase="bonus" if attempt.number == 16 else "main")


@router.post("/{run_id}/answer", response_model=AnswerFeedback)
def answer(run_id: UUID, payload: AnswerInput, request: Request,
           user: User = Depends(authenticated_user),
           db: Session = Depends(get_db)):
    validate_origin(request)
    try:
        result, correct_key = record_answer(
            db, run_id=run_id, user_id=user.id,
            question_id=payload.question_id, selected=payload.answer,
        )
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict:
        raise HTTPException(status_code=409, detail="No matching unanswered question") from None
    run = _lookup(db, run_id, user.id)
    return AnswerFeedback(
        correct=result.correct, correct_answer=correct_key,
        phase=result.phase, correct_main_answers=run.correct_main_answers,
        achievement=result.achievement,
    )
