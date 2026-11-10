"""Authenticated, PostgreSQL-backed v2 game endpoints.

The legacy prototype keeps /api/games until the Angular UI is switched.
These endpoints use request-scoped SQLAlchemy sessions and row-level locking
for irreversible answer transitions.
"""
import os
import random
import secrets
from uuid import UUID, NAMESPACE_URL, uuid5
from datetime import datetime, timezone

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
from persistence.models import GameRun, Question, QuestionAttempt, StarAward, Topic, User, WalletTransaction
from persistence.seed_topics import category_slug
from persistence.star_economy import lock_user_stars
from persistence.bonus_wager import accept_bonus, decline_bonus
from platinum_helper.service import get_purchase, card as helper_card
from question_schema import Choice, PublicQuestion
from star_perks import PerkUnavailable

router = APIRouter(prefix="/api/v2/games", tags=["Games — PostgreSQL"])


class NewGame(BaseModel):
    language: str = "English"


def _replacement_id(run_id: UUID, user_id: UUID) -> UUID:
    """Stable successor ID: retrying an uncertain replacement cannot create
    more than one new round, including after the client reloads.
    """
    return uuid5(NAMESPACE_URL, f"ai-quiz-game/replace/{user_id}/{run_id}")


def _replay_id(run_id: UUID, user_id: UUID) -> UUID:
    """Deterministic new run for a terminal round; safe across HTTP retries."""
    return uuid5(NAMESPACE_URL, f"ai-quiz-game/replay/{user_id}/{run_id}")


def _abandon_unpaid_legacy_runs(
    db: Session, *, user_id: UUID, exclude_id: UUID | None = None,
) -> None:
    """Close duplicate historical rounds only if no paid perks are at risk.

    Caller must hold the User row lock. Never silently discard a helper,
    purchased exclusions, or other spent stars: direct the player to Resume
    or the existing explicit abandon confirmation instead.
    """
    active = list(db.scalars(select(GameRun).where(
        GameRun.user_id == user_id,
        GameRun.status.in_(("main", "bonus")),
        GameRun.id != exclude_id if exclude_id is not None else True,
    ).order_by(GameRun.started_at.desc(), GameRun.id.desc()).with_for_update()))
    for run in active:
        if get_purchase(db, run.id) is not None or db.scalar(
            select(WalletTransaction.id).where(
                WalletTransaction.user_id == user_id,
                WalletTransaction.run_id == run.id,
                WalletTransaction.amount < 0,
                WalletTransaction.reason.like("star/%"),
            ).limit(1)
        ) is not None:
            raise HTTPException(status_code=409, detail={
                "code": "saved_paid_round",
                "game_id": str(run.id),
                "message": "Another unfinished round has paid perks; resume or explicitly replace it.",
            })
    for run in active:
        run.status = "abandoned"
        run.finished_at = datetime.now(timezone.utc)
    if active:
        db.flush()  # Free the partial UNIQUE(active user) slot before INSERT.


def _topics_ready(db: Session) -> bool:
    existing = set(db.scalars(select(Topic.slug)))
    return {category_slug(name) for name in QUESTION_CATEGORIES}.issubset(existing)


def _active_run(db: Session, user_id: UUID) -> GameRun | None:
    """Authoritative latest unfinished round, independent of browser storage.

    Historic versions could leave several open runs. Select the most recent
    rather than treating that legacy state as an error or hiding every run.
    """
    return db.scalar(
        select(GameRun).where(
            GameRun.user_id == user_id,
            GameRun.status.in_(("main", "bonus")),
        ).order_by(GameRun.started_at.desc(), GameRun.id.desc()).limit(1)
    )


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
    round_stars: dict[str, int]
    main_question_index: int = 0
    skipped_questions: int = 0
    question_issued: bool = False
    bonus_decision_available: bool = False
    helper: dict | None = None
    replacement_game_id: UUID | None = None


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
    purchase = get_purchase(db, run.id)
    skipped = db.scalar(select(func.count(QuestionAttempt.id)).where(
        QuestionAttempt.run_id == run.id,
        QuestionAttempt.number <= 15,
        QuestionAttempt.answered_at.is_not(None),
        QuestionAttempt.selected_key.is_(None),
        QuestionAttempt.is_correct.is_(None),
    )) or 0
    # Use the immutable awards for THIS run, not the account's lifetime
    # balance or a guessed reward based on the score. F5 must reconstruct Q16.
    round_stars = {tier: 0 for tier in ("bronze", "silver", "gold", "platinum")}
    for tier, count in db.execute(
        select(StarAward.tier, func.count(StarAward.id))
        .where(StarAward.run_id == run.id, StarAward.user_id == run.user_id)
        .group_by(StarAward.tier)
    ):
        round_stars[tier] = int(count)
    # A lost Q16 wager consumes this round's gold for the results display,
    # though the historical StarAward stays immutable.
    if run.bonus_wager_accepted and run.bonus_correct is False:
        round_stars["gold"] = max(0, round_stars["gold"] - 1)
    return GameInfo(
        game_id=run.id, language=run.language, phase=run.status,
        correct_main_answers=run.correct_main_answers,
        round_stars=round_stars,
        main_question_index=state.main_question_index,
        skipped_questions=int(skipped),
        question_issued=bool(db.scalar(select(func.count(QuestionAttempt.id)).where(
            QuestionAttempt.run_id == run.id
        ))),
        bonus_decision_available=(
            run.status == "bonus"
            and not run.bonus_wager_accepted
            and pending_question(db, run_id=run.id) is None
        ),
        helper=helper_card(purchase) if purchase else None,
        replacement_game_id=(
            _replacement_id(run.id, run.user_id)
            if run.status == "abandoned" and db.get(
                GameRun, _replacement_id(run.id, run.user_id)
            ) is not None else None
        ),
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



def _alternate_main_category(db: Session, run: GameRun, number: int,
                             primary: str) -> str | None:
    """One deterministic, allowed, unconsumed backup for a late main slot.

    Do not mutate the game or charge a paid category-change perk. A question
    only consumes a topic after it is actually offered and answered.
    """
    allowed = list(allowed_categories(run))
    available = set(available_categories(db, run, number))
    order = random.Random(run.id.int).sample(allowed, k=len(allowed))
    offset = (number - 1) % len(order)
    rotated = order[offset:] + order[:offset]
    return next((item for item in rotated
                 if item != primary and item in available), None)


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
    if not _topics_ready(db):
        raise HTTPException(status_code=503, detail="Topic catalog not initialized")
    # Serialize creation with replacement and purchases. Returning a conflict
    # rather than silently creating a second run protects already-paid perks.
    lock_user_stars(db, user.id)
    if _active_run(db, user.id) is not None:
        db.rollback()
        raise HTTPException(status_code=409, detail="Active game already exists")
    run = create_game(db, user_id=user.id, language=payload.language)
    return _game_info(db, run)


@router.post("/{run_id}/replay", response_model=GameInfo, status_code=201)
def replay_game(run_id: UUID, payload: NewGame, request: Request,
                user: User = Depends(authenticated_user),
                db: Session = Depends(get_db)):
    """'Play again' is a new empty run, not a rediscovery of older sessions.

    The terminal source run provides idempotency across retries. Obsolete
    unpaid runs may be abandoned by this explicit action, but purchased
    helpers and other paid perks require the existing Resume/Replace choice.
    """
    validate_origin(request)
    if payload.language not in LANGUAGE_DICT.values():
        raise HTTPException(status_code=422, detail="Unsupported language")
    try:
        lock_user_stars(db, user.id)
        finished = owned_game(db, run_id=run_id, user_id=user.id, for_update=True)
        if finished.status not in ("lost", "completed"):
            raise HTTPException(status_code=409, detail="Only finished rounds can be replayed")
        successor_id = _replay_id(finished.id, user.id)
        successor = db.get(GameRun, successor_id)
        if successor is not None:
            result = _game_info(db, successor)
            db.commit()
            return result
        if not _topics_ready(db):
            raise HTTPException(status_code=503, detail="Topic catalog not initialized")
        _abandon_unpaid_legacy_runs(db, user_id=user.id)
        successor = GameRun(
            id=successor_id, user_id=user.id,
            language=payload.language, status="main",
        )
        db.add(successor)
        db.flush()
        result = _game_info(db, successor)
        db.commit()
        return result
    except GameNotFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="Game not found") from None
    except Exception:
        db.rollback()
        raise


@router.get("/active", response_model=GameInfo | None)
def active_game(user: User = Depends(authenticated_user),
                db: Session = Depends(get_db)):
    """Return the signed-in user's unfinished round (or JSON null).

    The database, not a client-controlled localStorage key, determines which
    run is resumable. Includes pre-Q1 purchases and bonus decision state.
    """
    run = _active_run(db, user.id)
    info = _game_info(db, run) if run is not None else None
    from platinum_helper.debug import active_round as debug_active_round
    debug_active_round(info)
    return info


@router.post("/{run_id}/replace", response_model=GameInfo, status_code=201)
def replace_game(run_id: UUID, payload: NewGame, request: Request,
                 user: User = Depends(authenticated_user),
                 db: Session = Depends(get_db)):
    """Confirm abandonment and create an unstarted replacement as ONE transaction.

    No rewards, debits, or paid perks move across rounds. Replays of the same
    replacement return its existing ID, including after a network timeout.
    The old run's row is serialized with answers/purchases before changing status.
    """
    validate_origin(request)
    if payload.language not in LANGUAGE_DICT.values():
        raise HTTPException(status_code=422, detail="Unsupported language")
    try:
        # Consistent lock order with answers and paid perks: User then GameRun.
        from persistence.star_economy import lock_user_stars
        lock_user_stars(db, user.id)
        run = owned_game(db, run_id=run_id, user_id=user.id, for_update=True)
        successor_id = _replacement_id(run.id, user.id)
        successor = db.get(GameRun, successor_id)
        if run.status == "abandoned" and successor is not None:
            result = _game_info(db, successor)
            db.commit()
            return result
        if run.status not in ("main", "bonus"):
            raise HTTPException(status_code=409, detail="Round is no longer active")
        if not _topics_ready(db):
            raise HTTPException(status_code=503, detail="Topic catalog not initialized")
        if successor is not None:
            raise HTTPException(status_code=409, detail="Replacement round already exists")
        _abandon_unpaid_legacy_runs(db, user_id=user.id, exclude_id=run.id)
        run.status = "abandoned"
        run.finished_at = datetime.now(timezone.utc)
        db.flush()  # UPDATE inactive before inserting the only new active run.
        successor = GameRun(
            id=successor_id, user_id=user.id,
            language=payload.language, status="main",
        )
        db.add(successor)
        db.flush()
        result = _game_info(db, successor)
        db.commit()
        return result
    except GameNotFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="Game not found") from None
    except Exception:
        db.rollback()
        raise


@router.get("/{run_id}", response_model=GameInfo)
def game_info(run_id: UUID, user: User = Depends(authenticated_user),
              db: Session = Depends(get_db)):
    run = _lookup(db, run_id, user.id)
    return _game_info(db, run)


@router.patch("/{run_id}/language", response_model=GameInfo)
def change_pregame_language(run_id: UUID, payload: NewGame, request: Request,
                            user: User = Depends(authenticated_user),
                            db: Session = Depends(get_db)):
    """Change language on this existing run, preserving paid exclusions.

    A row lock and issued-attempt check prevent changes once Q1 exists.
    Does not create another game, charge stars or modify the allowlist.
    """
    validate_origin(request)
    if payload.language not in LANGUAGE_DICT.values():
        raise HTTPException(status_code=422, detail="Unsupported language")
    try:
        run = owned_game(db, run_id=run_id, user_id=user.id, for_update=True)
        issued = db.scalar(select(func.count(QuestionAttempt.id)).where(
            QuestionAttempt.run_id == run_id
        )) or 0
        if run.status != "main" or issued:
            raise HTTPException(status_code=409, detail="Question language is locked after Q1")
        run.language = payload.language
        db.commit()
        return _game_info(db, run)
    except GameNotFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="Game not found") from None
    except Exception:
        db.rollback()
        raise


@router.get("/{run_id}/question", response_model=PublicQuestion)
def get_question(run_id: UUID, user: User = Depends(authenticated_user),
                 db: Session = Depends(get_db)):
    run = _lookup(db, run_id, user.id)
    if run.status not in ("main", "bonus"):
        raise HTTPException(status_code=409, detail="Game has ended")

    cached = pending_question(db, run_id=run.id)
    if cached is not None and cached[1].status != "approved":
        # Another player's report quarantined a previously-issued question.
        # Do not re-serve it on F5; retract without advancing this round.
        # Lock game -> attempt and re-check under the transaction.
        locked_run = owned_game(db, run_id=run_id, user_id=user.id, for_update=True)
        cached = pending_question(db, run_id=locked_run.id)
        if cached is not None and cached[1].status != "approved":
            old = db.scalar(select(QuestionAttempt).where(
                QuestionAttempt.id == cached[0].id).with_for_update())
            if old is not None and old.answered_at is None and old.superseded_at is None:
                old.superseded_at = datetime.now(timezone.utc)
                db.commit()
            cached = None
    if cached is not None:
        event("question_cache", hit=True)
        return _public(*cached, phase=run.status, db=db)

    if run.status == "bonus" and not run.bonus_wager_accepted:
        raise HTTPException(status_code=409, detail="Choose whether to risk your gold before Q16")
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

    # Keep the original v2 path for legacy clients; tagged games have
    # strict quality gates, emergency pool reuse and late-game recovery.
    question_number = 16 if run.status == "bonus" else game_progress.main_question_index + 1
    tagged = os.getenv("AI_TAG_DEDUP_ENABLED", "false").strip().lower() == "true"
    if tagged:
        from tagging.core import HistoryNotTagged, generator_context
        from tagging.repository import history_for_user
        from tagging.review import is_strict_too_easy_reserve
        from persistence.question_pool import (
            enabled as fallback_enabled, offer_approved_pool_question, scan_limit,
        )
        try:
            history = history_for_user(db, user.id)
        except HistoryNotTagged:
            raise HTTPException(
                status_code=503,
                detail="Historical questions lack verified tags; run tagging.backfill",
            ) from None

        # Q10–Q15 may try one other unused allowed category automatically.
        # Q16 uses its existing *persisted* bonus rotation and the same cap.
        categories_to_try = [category]
        if 10 <= question_number <= 15:
            alternate = _alternate_main_category(db, run, question_number, category)
            if alternate is not None:
                categories_to_try.append(alternate)
        elif question_number == 16:
            categories_to_try.append(None)  # Resolve after persisted rotation.
        db.rollback()  # Never hold a DB connection while calling AI.

        pool_settings = None
        reserves = []
        last_generation_error = None

        def offer_pool(candidate_category: str):
            if not pool_settings[0]:
                return None
            try:
                return offer_approved_pool_question(
                    db, run_id=run_id, user_id=user.id,
                    category=candidate_category, max_candidates=pool_settings[2],
                    expected_bonus_topic_index=bonus_index,
                )
            except Exception as exc:
                # The optional pool must never bypass quality checks or
                # mutate an already issued question when it fails.
                event("question_pool_fallback_failed", error_type=type(exc).__name__)
                db.rollback()
                return None

        for category_attempt, proposed_category in enumerate(categories_to_try):
            if category_attempt and question_number == 16:
                previous_index = bonus_index
                _advance_bonus_topic_after_failure(
                    db, run_id=run_id, user_id=user.id,
                    expected_topic_index=previous_index,
                    failure_status=last_generation_error.status_code,
                )
                run = _lookup(db, run_id, user.id)
                cached = pending_question(db, run_id=run.id)
                if cached is not None:
                    return _public(*cached, phase=run.status, db=db)
                bonus_index = run.bonus_topic_index
                if bonus_index == previous_index:
                    raise last_generation_error
                proposed_category = _category_for_round(
                    run_id, question_number, bonus_index, db=db,
                )
                db.rollback()
            selected_category = proposed_category
            if category_attempt:
                if selected_category == category:
                    # No actual alternative was available; avoid a duplicate
                    # paid provider call with the same topic.
                    raise last_generation_error
                event("question_category_retry_selected",
                      number=question_number, category_attempt=category_attempt + 1,
                      from_category=category, to_category=selected_category)
                # Search the fully approved pool in the new category BEFORE
                # making more paid model requests.
                pooled = offer_pool(selected_category)
                if pooled is not None:
                    return _public(
                        *pooled, phase="bonus" if pooled[0].number == 16 else "main",
                        db=db,
                    )
            exclusions = generator_context(history, category_slug(selected_category))
            db.rollback()
            too_easy_reserve = []
            try:
                candidate = generate_valid_question(
                    difficulty, language, selected_category,
                    tag_history=history, tag_context=exclusions,
                    too_easy_reserve=too_easy_reserve,
                    question_number=question_number,
                )
            except HTTPException as generation_error:
                recoverable = (generation_error.status_code in (502, 503)
                               or (generation_error.status_code == 409
                                   and generation_error.detail == {"code": "duplicate_exhausted"}))
                if not recoverable:
                    _advance_bonus_topic_after_failure(
                        db, run_id=run_id, user_id=user.id,
                        expected_topic_index=bonus_index,
                        failure_status=generation_error.status_code,
                    )
                    raise
                if pool_settings is None:
                    try:
                        use_pool = fallback_enabled("QUIZ_QUESTION_POOL_FALLBACK_ENABLED")
                        use_reserve = (
                            question_number != 16
                            and fallback_enabled("QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED")
                        )
                        limit = scan_limit() if use_pool else 1
                        pool_settings = (use_pool, use_reserve, limit)
                    except ValueError:
                        event("question_fallback_configuration_failed")
                        raise HTTPException(
                            status_code=503, detail="Invalid question fallback configuration"
                        ) from None
                if category_attempt == 0:
                    pooled = offer_pool(selected_category)
                    if pooled is not None:
                        return _public(
                            *pooled, phase="bonus" if pooled[0].number == 16 else "main",
                            db=db,
                        )
                if pool_settings[1]:
                    eligible = next((item for item in too_easy_reserve
                                     if is_strict_too_easy_reserve(item)), None)
                    if eligible is not None:
                        reserves.append((selected_category, eligible, generation_error))
                last_generation_error = generation_error
                if category_attempt + 1 < len(categories_to_try):
                    continue
                if reserves:
                    category, candidate, original_error = reserves[0]
                    # Strict too-easy-only reserve stays the absolute LAST
                    # fallback, after both categories and their approved pools.
                    report = candidate._review_report
                    report["details"]["fallback"] = {
                        "reason": "too_easy_only",
                        "original_decision": report["decision"],
                        "generation_status": original_error.status_code,
                    }
                    report["decision"] = "fallback_too_easy"
                    event("question_too_easy_fallback", outcome="selected",
                          requested_difficulty=difficulty)
                    break
                # Q16 has already rotated once to its second category.
                # Keep that index for the next GET retry, without charging
                # any perk or losing the active round.
                if question_number == 16 and category_attempt == 0:
                    _advance_bonus_topic_after_failure(
                        db, run_id=run_id, user_id=user.id,
                        expected_topic_index=bonus_index,
                        failure_status=generation_error.status_code,
                    )
                raise
            else:
                category = selected_category
                break
    else:
        try:
            candidate = generate_valid_question(
                difficulty, language, category, question_number=question_number,
            )
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
            difficulty, language, chosen, tag_history=history, tag_context=context,
            question_number=attempt.number)
    else:
        candidate = generate_valid_question(
            difficulty, language, chosen, question_number=attempt.number,
        )
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


@router.post("/{run_id}/bonus/accept", response_model=GameInfo)
def accept_optional_bonus(run_id: UUID, request: Request,
                          user: User = Depends(authenticated_user),
                          db: Session = Depends(get_db)):
    validate_origin(request)
    try:
        run = accept_bonus(db, user_id=user.id, run_id=run_id)
        return _game_info(db, run)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/{run_id}/bonus/decline", response_model=GameInfo)
def decline_optional_bonus(run_id: UUID, request: Request,
                           user: User = Depends(authenticated_user),
                           db: Session = Depends(get_db)):
    validate_origin(request)
    try:
        run = decline_bonus(db, user_id=user.id, run_id=run_id)
        return _game_info(db, run)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
