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


def _category_for_round(run_id: UUID, number: int) -> str:
    """Stable per-round shuffled category order, surviving process restart."""
    if not 1 <= number <= 16:
        raise GameConflict("Invalid round question number")
    categories = random.Random(run_id.int).sample(
        QUESTION_CATEGORIES, k=len(QUESTION_CATEGORIES)
    )
    return categories[number - 1]


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
    category = _category_for_round(run.id, run.correct_main_answers + 1)
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

    candidate = generate_valid_question(difficulty, language, category)
    provider, model = _generation_metadata()
    try:
        attempt, stored = offer_question(
            db, run_id=run_id, user_id=user.id, question=candidate,
            category=category, provider=provider, model=model,
        )
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict:
        raise HTTPException(status_code=409, detail="Game state changed; reload question") from None
    except TopicsNotSeeded:
        raise HTTPException(status_code=503, detail="Topic catalog not initialized") from None

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
