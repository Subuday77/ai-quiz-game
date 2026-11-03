"""Authenticated, PostgreSQL-backed v2 game endpoints.

The legacy prototype keeps /api/games until the Angular UI is switched.
These endpoints use request-scoped SQLAlchemy sessions and row-level locking
for irreversible answer transitions.
"""
import os
import random
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from consts import LANGUAGE_DICT, QUESTION_CATEGORIES
from diagnostics import event
from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.gameplay import (
    GameConflict, GameNotFound, TopicsNotSeeded, create_game, game_state,
    offer_question, owned_game, pending_question, record_answer,
    skip_main_question, hide_wrong_options, hidden_choices,
)
from persistence.categories import (
    allowed_categories, available_categories, category_snapshot,
    exclude_pregame_categories,
)
from persistence.models import Question, QuestionAttempt, Topic, User
from persistence.seed_topics import category_slug
from question_schema import Choice, PublicQuestion
from star_perks import PerkUnavailable

router = APIRouter(prefix="/api/v2/games", tags=["Games — PostgreSQL"])


class NewGame(BaseModel):
    language: str = "English"


class AnswerInput(BaseModel):
    question_id: UUID
    answer: Choice


class SkipInput(BaseModel):
    question_id: UUID
    request_id: UUID


class RemoveInput(BaseModel):
    question_id: UUID
    count: int = Field(ge=1, le=2)
    request_id: UUID


class ExcludeInput(BaseModel):
    categories: list[str] = Field(min_length=1)
    request_id: UUID


class ChangeCategoryInput(BaseModel):
    question_id: UUID
    request_id: UUID
    mode: str  # random | manual
    category: str | None = None


class GameInfo(BaseModel):
    game_id: UUID
    language: str
    phase: str
    correct_main_answers: int
    main_question_index: int = 0
    skipped_questions: int = 0


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


def _game_info(db: Session, run) -> GameInfo:
    """The progress bar follows consumed slots, not the number of right answers.

    Skips are distinguished from ordinary answers by the NULL answer markers.
    Both counts come from persisted attempts and survive page reloads.
    """
    state = game_state(run, db)
    skipped = db.scalar(select(func.count(QuestionAttempt.id)).where(
        QuestionAttempt.run_id == run.id,
        QuestionAttempt.number <= 15,
        QuestionAttempt.answered_at.is_not(None),
        QuestionAttempt.selected_key.is_(None),
        QuestionAttempt.is_correct.is_(None),
    )) or 0
    return GameInfo(
        game_id=run.id, language=run.language, phase=run.status,
        correct_main_answers=run.correct_main_answers,
        main_question_index=state.main_question_index,
        skipped_questions=int(skipped),
    )


def _public(attempt, stored, phase: str, *, db: Session | None = None) -> PublicQuestion:
    # Preserve the original keys and canonical A/B/C/D order even after hints.
    # Never compact survivors into A/B/C; the displayed letters are part of
    # the question's immutable answer mapping.
    hidden = set()
    if attempt.hints_used:
        if db is None:
            raise GameConflict("Database needed for persisted question hints")
        hidden = set(hidden_choices(db, attempt, stored))
    options = {key: stored.choices[key] for key in "ABCD" if key not in hidden}
    return PublicQuestion(
        question_id=str(attempt.question_id),
        number=attempt.number,
        difficulty=attempt.requested_difficulty,
        question=stored.text,
        answers=options,
        bonus=phase == "bonus",
        hidden_count=attempt.hints_used,
    )


def _category_for_round(run_id: UUID, number: int,
                        bonus_topic_index: int = 0,
                        db: Session | None = None) -> str:
    """Deterministic category order, restricted to the per-game allowlist.

    Previously *answered* categories are consumed. A paid category rejection
    or a gold skip is eligible in a future slot; all resets use the allowlist.
    db=None retains the original pure legacy/test calculation.
    """
    if not 1 <= number <= 16:
        raise GameConflict("Invalid round question number")
    if db is None:
        categories = random.Random(run_id.int).sample(
            QUESTION_CATEGORIES, k=len(QUESTION_CATEGORIES))
        return categories[(15 + bonus_topic_index) % len(categories)] if number == 16 else categories[number - 1]
    from persistence.categories import allowed_categories, available_categories
    from persistence.models import GameRun
    run = db.get(GameRun, run_id)
    if run is None:
        raise GameNotFound("Game not found")
    allowed = list(allowed_categories(run))
    categories = random.Random(run_id.int).sample(allowed, k=len(allowed))
    unused = set(available_categories(db, run, number))
    base = (15 + bonus_topic_index) if number == 16 else number - 1
    # Bonus retry walks the available-first order. Once all unused topics
    # have been tried, cycle only within this player's permitted categories.
    # With the default complete catalog, the order is identical to v2.
    if number == 16:
        rotation = categories[15:] + categories[:15]
        ordered = ([cat for cat in rotation if cat in unused] +
                   [cat for cat in rotation if cat not in unused])
        return ordered[bonus_topic_index % len(ordered)]
    rotated = categories[base % len(categories):] + categories[:base % len(categories)]
    return next((category for category in rotated if category in unused),
                rotated[0])


def _advance_bonus_topic_after_failure(
    db: Session, *, run_id: UUID, user_id: UUID,
    expected_topic_index: int | None, failure_status: int,
) -> None:
    """Rotate the server's Q16 topic only after failed generation.

    The NEXT normal GET /question uses the next topic, including on Retry.
    No player-facing endpoint or extra paid provider call. The choice
    persists, cycles indefinitely, and is guarded against concurrent
    failures and already-issued questions.
    """
    if expected_topic_index is None or failure_status not in (409, 502, 503):
        return
    db.rollback()
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if (run.status != "bonus" or run.correct_main_answers != 15
                or run.bonus_topic_index != expected_topic_index
                or pending_question(db, run_id=run.id) is not None):
            db.rollback()
            event("bonus_topic_rotation_skipped", reason="state_changed")
            return
        from persistence.categories import allowed_categories
        next_index = (run.bonus_topic_index + 1) % len(allowed_categories(run))
        old_index = run.bonus_topic_index
        run.bonus_topic_index = next_index
        # Retain for the future star-perk policy, but do not offer players
        # topic selection in this iteration.
        run.bonus_topic_change_unlocked = True
        db.commit()
        event("bonus_topic_auto_rotated", from_index=old_index,
              to_index=next_index, cycle_reset=(next_index == 0),
              failure_status=failure_status)
    except Exception as exc:
        db.rollback()
        event("bonus_topic_rotation_failed", error_type=type(exc).__name__)


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
    return _game_info(db, run)


@router.get("/{run_id}", response_model=GameInfo)
def game_info(run_id: UUID, user: User = Depends(authenticated_user),
              db: Session = Depends(get_db)):
    run = _lookup(db, run_id, user.id)
    return _game_info(db, run)


@router.get("/{run_id}/question", response_model=PublicQuestion)
def get_question(run_id: UUID, user: User = Depends(authenticated_user),
                 db: Session = Depends(get_db)):
    run = _lookup(db, run_id, user.id)
    if run.status not in ("main", "bonus"):
        raise HTTPException(status_code=409, detail="Game has ended")

    cached = pending_question(db, run_id=run.id)
    if cached is not None:
        event("question_cache", hit=True)
        return _public(*cached, phase=run.status, db=db)

    event("question_cache", hit=False)
    game_progress = game_state(run, db)
    difficulty = game_progress.current_difficulty
    bonus_index = run.bonus_topic_index if run.status == "bonus" else None
    category = _category_for_round(
        run.id, (16 if run.status == "bonus" else game_progress.main_question_index + 1),
        bonus_index or 0, db=db
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
                _advance_bonus_topic_after_failure(db, run_id=run_id, user_id=user.id, expected_topic_index=bonus_index, failure_status=generation_error.status_code)
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
                raise HTTPException(
                    status_code=503, detail="Invalid question fallback configuration"
                ) from None
            if use_pool:
                try:
                    pooled = offer_approved_pool_question(
                        db, run_id=run_id, user_id=user.id,
                        category=category, max_candidates=limit,
                        expected_bonus_topic_index=bonus_index,
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
                            db=db,
                        )
            if not use_reserve or not too_easy_reserve:
                _advance_bonus_topic_after_failure(db, run_id=run_id, user_id=user.id, expected_topic_index=bonus_index, failure_status=generation_error.status_code)
                raise
            candidate = next(
                (item for item in too_easy_reserve
                 if is_strict_too_easy_reserve(item)), None,
            )
            if candidate is None:
                _advance_bonus_topic_after_failure(db, run_id=run_id, user_id=user.id, expected_topic_index=bonus_index, failure_status=generation_error.status_code)
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
        except HTTPException as generation_error:
            _advance_bonus_topic_after_failure(
                db, run_id=run_id, user_id=user.id,
                expected_topic_index=bonus_index,
                failure_status=generation_error.status_code,
            )
            raise
    provider, model = _generation_metadata()
    try:
        attempt, stored = offer_question(
            db, run_id=run_id, user_id=user.id, question=candidate,
            category=category, provider=provider, model=model,
            prompt_version="tagged-v2" if tagged else "v1",
            schema_version="question_tagged_v3" if tagged else "question_v1",
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
    return _public(attempt, stored, phase="bonus" if attempt.number == 16 else "main", db=db)


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


@router.post("/{run_id}/skip", response_model=GameInfo)
def skip_question(run_id: UUID, payload: SkipInput, request: Request,
                  user: User = Depends(authenticated_user),
                  db: Session = Depends(get_db)):
    """Paid gold skip; does not count as a correct answer or expose the key."""
    validate_origin(request)
    try:
        skip_main_question(db, run_id=run_id, user_id=user.id,
                           question_id=payload.question_id,
                           request_id=payload.request_id)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (GameConflict, PerkUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    run = _lookup(db, run_id, user.id)
    return _game_info(db, run)


@router.post("/{run_id}/hints/remove", response_model=PublicQuestion)
def remove_distractors(run_id: UUID, payload: RemoveInput, request: Request,
                       user: User = Depends(authenticated_user),
                       db: Session = Depends(get_db)):
    """2 bronze for one / 3 bronze for two; no hint on bonus Q16."""
    validate_origin(request)
    try:
        attempt, question = hide_wrong_options(
            db, run_id=run_id, user_id=user.id, question_id=payload.question_id,
            count=payload.count, request_id=payload.request_id,
        )
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (GameConflict, PerkUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _public(attempt, question, phase="main", db=db)


@router.get("/{run_id}/categories")
def categories(run_id: UUID, user: User = Depends(authenticated_user),
               db: Session = Depends(get_db)):
    """Available per-game topics; no private question or correct key."""
    return category_snapshot(db, _lookup(db, run_id, user.id))


@router.post("/{run_id}/categories/exclude")
def exclude_categories(run_id: UUID, payload: ExcludeInput, request: Request,
                       user: User = Depends(authenticated_user),
                       db: Session = Depends(get_db)):
    """Before Q1, pay five silver for every newly excluded category."""
    validate_origin(request)
    try:
        exclude_pregame_categories(
            db, run_id=run_id, user_id=user.id,
            categories=payload.categories, request_id=payload.request_id)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (GameConflict, PerkUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return category_snapshot(db, _lookup(db, run_id, user.id))


@router.post("/{run_id}/categories/change")
def change_category(run_id: UUID, payload: ChangeCategoryInput, request: Request,
                    user: User = Depends(authenticated_user),
                    db: Session = Depends(get_db)):
    """Pay silver only after a replacement question is successfully validated.

    Generator and critic operate without held DB locks; the final transaction
    verifies attempt identity, caps, category uniqueness, user balance, then
    commits the new version and debit together. A failed AI call costs zero.
    """
    validate_origin(request)
    from star_perks import category_change_price
    if payload.mode not in ("random", "manual"):
        raise HTTPException(status_code=422, detail="Invalid category mode")
    manual = payload.mode == "manual"
    if manual != (payload.category is not None):
        raise HTTPException(status_code=422, detail="Manual mode requires category only")
    run = _lookup(db, run_id, user.id)
    if run.status != "main":
        raise HTTPException(status_code=409, detail="No category changes on bonus/finished games")
    pending = pending_question(db, run_id=run_id)
    if pending is None or pending[0].question_id != payload.question_id:
        raise HTTPException(status_code=409, detail="No matching unanswered question")
    attempt, current_question = pending
    snapshot = category_snapshot(db, run)
    try:
        category_change_price(
            allowed_count=len(allowed_categories(run)),
            changes_used=attempt.category_change_count,
            manual_used=attempt.manual_category_used,
            manual=manual,
        )
    except PerkUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    options = snapshot["alternatives"]
    if not options:
        raise HTTPException(status_code=409, detail="No unused categories available")
    if manual and payload.category not in options:
        raise HTTPException(status_code=409, detail="Category is not available")
    chosen = payload.category if manual else secrets.choice(options)
    old_attempt_id = attempt.id
    difficulty = attempt.requested_difficulty
    language = run.language
    db.rollback()  # Never keep a PostgreSQL lock/connection during AI calls.

    from api import generate_valid_question
    tagged = os.getenv("AI_TAG_DEDUP_ENABLED", "false").strip().lower() == "true"
    if tagged:
        from tagging.core import HistoryNotTagged, generator_context
        from tagging.repository import history_for_user
        try:
            history = history_for_user(db, user.id)
        except HistoryNotTagged:
            raise HTTPException(status_code=503, detail="Historical tags required") from None
        context = generator_context(history, category_slug(chosen))
        db.rollback()
        candidate = generate_valid_question(
            difficulty, language, chosen, tag_history=history, tag_context=context)
    else:
        candidate = generate_valid_question(difficulty, language, chosen)
    provider, model = _generation_metadata()
    try:
        new_attempt, stored = offer_question(
            db, run_id=run_id, user_id=user.id, question=candidate,
            category=chosen, provider=provider, model=model,
            prompt_version="tagged-v2" if tagged else "v1",
            schema_version="question_tagged_v3" if tagged else "question_v1",
            dedup_enforce=tagged,
            replace_question_id=payload.question_id,
            replace_attempt_id=old_attempt_id,
            replace_manual=manual,
            category_operation_key=f"star:{user.id}:{payload.request_id}:category",
        )
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (GameConflict, PerkUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _public(new_attempt, stored, phase="main", db=db)
