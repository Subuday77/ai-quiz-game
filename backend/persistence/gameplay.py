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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from game_logic import AnswerResult, GameState
from persistence.models import GameRun, Question, QuestionAttempt, QuestionReview, StarAward, Topic, User
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


def game_state(run: GameRun, db: Session | None = None) -> GameState:
    """Rebuild progression; skipped slots differ from correct answer count.

    A completed attempt with NULL selected_key / is_correct is a paid skip.
    No new database column is required: count completed main question slots.
    db=None preserves existing non-skip callers and tests.
    """
    slot_index = run.correct_main_answers
    if db is not None:
        slot_index = max(run.correct_main_answers, int(db.scalar(select(func.count(QuestionAttempt.id)).where(
            QuestionAttempt.run_id == run.id,
            QuestionAttempt.number <= 15,
            QuestionAttempt.answered_at.is_not(None),
        )) or 0))
    return GameState(
        correct_main_answers=run.correct_main_answers,
        main_question_index=slot_index,
        hints_used=run.hints_used,
        phase=run.status,
        bonus_correct=run.bonus_correct,
    )


def pending_question(db: Session, *, run_id: UUID) -> tuple[QuestionAttempt, Question] | None:
    attempt = db.scalar(
        select(QuestionAttempt)
        .where(QuestionAttempt.run_id == run_id,
               QuestionAttempt.answered_at.is_(None),
               QuestionAttempt.superseded_at.is_(None))
        .order_by(QuestionAttempt.number.desc(), QuestionAttempt.revision.desc())
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
    replace_question_id: UUID | None = None,
    replace_attempt_id: UUID | None = None,
    replace_manual: bool = False,
    category_operation_key: str | None = None,
) -> tuple[QuestionAttempt, Question]:
    """Store one already-validated candidate, or return the cached pending one.

    The row lock serializes concurrent requests for the same run on PostgreSQL.
    Model calls should happen OUTSIDE this transaction, before invoking this
    function, so slow generation does not hold the game row lock.
    """
    try:
        if dedup_enforce or replace_question_id is not None:
            # Global lock order: player before run for all debits and tagged dedup.
            db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        if dedup_enforce:
            from tagging.contract import TaggedQuestion
            from tagging.core import require_unique
            from tagging.repository import history_for_user
            if not isinstance(question, TaggedQuestion):
                raise GameConflict("Tagged workflow requires verified fact metadata")
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game has ended")
        # A topic switch may happen while the old AI request is in flight.
        # Discard its late result rather than present a wrong-category bonus.
        if (run.status == "bonus" and expected_bonus_topic_index is not None
                and run.bonus_topic_index != expected_bonus_topic_index):
            raise GameConflict("Bonus topic changed during generation")
        cached = pending_question(db, run_id=run.id)
        replacing = replace_question_id is not None
        old_attempt = None
        switch_price = None
        if replacing:
            from persistence.models import WalletTransaction
            from star_perks import PerkUnavailable, category_change_price
            if not category_operation_key or replace_attempt_id is None:
                raise GameConflict("Missing category operation identity")
            if cached is None or cached[0].id != replace_attempt_id or cached[0].question_id != replace_question_id:
                # A correctly replayed request may arrive after its replacement
                # committed; it must not deduct again.
                prior = db.scalar(select(WalletTransaction).where(
                    WalletTransaction.idempotency_key == category_operation_key))
                if (prior is not None and cached is not None
                        and prior.user_id == user_id and prior.run_id == run_id
                        and prior.reason == ("star/silver/choose-category" if replace_manual
                                             else "star/silver/random-category")):
                    db.commit()
                    return cached
                raise GameConflict("Question changed during category generation")
            old_attempt, old_question = cached
            if run.status != "main":
                raise GameConflict("Category perks are limited to the main game")
            if category == db.get(Topic, old_question.topic_id).labels.get("en"):
                raise GameConflict("New category matches current category")
            allowed = run.allowed_categories if run.allowed_categories is not None else __import__("consts").QUESTION_CATEGORIES
            switch_price = category_change_price(
                allowed_count=len(allowed),
                changes_used=old_attempt.category_change_count,
                manual_used=old_attempt.manual_category_used,
                manual=replace_manual,
            )
            if category not in allowed:
                raise GameConflict("Category excluded from this game")
            previous_topics = set(db.scalars(select(Topic.slug)
                .join(Question, Question.topic_id == Topic.id)
                .join(QuestionAttempt, QuestionAttempt.question_id == Question.id)
                .where(QuestionAttempt.run_id == run_id,
                       QuestionAttempt.number == old_attempt.number)))
            if category_slug(category) in previous_topics:
                raise GameConflict("Category already used in the current slot")
        elif cached:
            db.commit()
            return cached
        if run.allowed_categories is not None and category not in run.allowed_categories:
            raise GameConflict("Category excluded from this game")
        if dedup_enforce:
            # Atomic recheck after another parallel request may have committed.
            require_unique(question, history_for_user(db, user_id))
        state = game_state(run, db)
        expected_difficulty = state.current_difficulty
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
                competence_area=(question._competence_area if dedup_enforce else None),
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
            # Old identical saved questions may lack this field; upgrade them
            # only after the new candidate passed the independent review.
            if stored.competence_area is None and question._competence_area is not None:
                stored.competence_area = question._competence_area
            from tagging.repository import save_metadata
            save_metadata(db, stored, question)
            # Generator estimates are recorded once, separately from quality
            # verdicts. Never ask Terra/the critic to approve these numbers.
            if question.plausibility is not None:
                score_record = db.scalar(select(QuestionReview.id).where(
                    QuestionReview.question_id == stored.id,
                    QuestionReview.checker_type == "generator_plausibility",
                ))
                if score_record is None:
                    db.add(QuestionReview(
                        question_id=stored.id,
                        checker_type="generator_plausibility",
                        provider=provider, model=model,
                        contract_version="generator_plausibility_v1",
                        decision="unverified_estimate",
                        issues=[], details={"scores": question.plausibility},
                    ))
            # The reviewer runs BEFORE the DB transaction; persist its full
            # accepted verdict alongside the question, tags and attempt.
            # PrivateAttr remains absent from all player-facing JSON.
            review = question._review_report
            if review is not None:
                db.add(QuestionReview(question_id=stored.id, **review))
        next_number = 16 if run.status == "bonus" else state.main_question_index + 1
        if replacing:
            from persistence.star_economy import spend_star
            # All expensive provider work is finished; spend and replace as a
            # single PostgreSQL transaction, with both player/run row locks.
            spend_star(
                db, user_id=user_id, run_id=run.id, tier="silver",
                amount=switch_price,
                action="choose-category" if replace_manual else "random-category",
                operation_key=category_operation_key,
            )
            old_attempt.superseded_at = _now()
            run.hints_used += 1
        attempt = QuestionAttempt(
            run_id=run.id, question_id=stored.id,
            number=next_number,
            requested_difficulty=expected_difficulty,
            revision=(old_attempt.revision + 1 if replacing else
                      int(db.scalar(select(func.coalesce(func.max(QuestionAttempt.revision), -1)).where(
                          QuestionAttempt.run_id == run.id,
                          QuestionAttempt.number == next_number,
                      ))) + 1),
            category_change_count=old_attempt.category_change_count + 1 if replacing else 0,
            manual_category_used=old_attempt.manual_category_used + (1 if replace_manual else 0) if replacing else 0,
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
        # All star operations acquire player then game locks. Keep the same
        # order for Q15 awards and Q16 wager settlement to avoid deadlocks.
        db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game has ended")
        was_bonus = run.status == "bonus"
        cached = pending_question(db, run_id=run.id)
        if cached is None:
            raise GameConflict("No matching unanswered question")
        attempt, question = cached
        if question.id != question_id or question.status != "approved":
            raise GameConflict("No matching approved unanswered question")

        now = _now()
        state = game_state(run, db)
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
        if run.correct_main_answers == 15 and state.main_question_index == 15:
            from platinum_helper.service import refund_if_earned
            refund_if_earned(db, run)
        if was_bonus and is_correct:
            from persistence.bonus_wager import credit_won_gold
            credit_won_gold(db, run=run)
        # If the wager is lost, its earlier debit remains and the earned
        # gold award stays untouched as an immutable achievement.
        db.commit()
        return result, question.correct_key
    except Exception:
        db.rollback()
        raise


def skip_main_question(
    db: Session, *, run_id: UUID, user_id: UUID,
    question_id: UUID, request_id: UUID,
) -> GameState:
    """Buy one gold skip; debit and question advancement commit together.

    The attempt is marked completed with no selected key/correctness. A
    deterministic idempotency key prevents a repeated HTTP request from
    accidentally skipping the following question.
    """
    from persistence.models import WalletTransaction
    from persistence.star_economy import lock_user_stars, spend_star
    from star_perks import PerkUnavailable

    operation_key = f"star:{user_id}:{request_id}:skip"
    try:
        # Consistent global lock order: user first, then game.
        lock_user_stars(db, user_id)
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        existing = db.scalar(select(WalletTransaction).where(
            WalletTransaction.idempotency_key == operation_key
        ))
        if existing is not None:
            if (existing.user_id != user_id or existing.run_id != run_id
                    or existing.reason != "star/gold/skip-question"
                    or existing.amount != -1):
                raise PerkUnavailable("Idempotency key reused")
            result = game_state(run, db)
            db.commit()
            return result
        if run.status != "main":
            raise GameConflict("Question skip is not available outside main rounds")
        cached = pending_question(db, run_id=run.id)
        if cached is None:
            raise GameConflict("No pending question to skip")
        attempt, _ = cached
        if attempt.question_id != question_id:
            raise GameConflict("Question has changed")

        spend_star(
            db, user_id=user_id, run_id=run_id, tier="gold", amount=1,
            action="skip-question", operation_key=operation_key,
        )
        state = game_state(run, db)
        state.skip_main_question()
        now = _now()
        attempt.answered_at = now
        attempt.selected_key = None
        attempt.is_correct = None
        run.hints_used = state.hints_used
        run.status = state.phase
        if state.phase == "completed":
            run.finished_at = now
        db.commit()
        return state
    except Exception:
        db.rollback()
        raise


def question_plausibility(db: Session, question_id: UUID) -> dict | None:
    """Read the original generator estimate; never request or infer a re-score."""
    review = db.scalar(select(QuestionReview).where(
        QuestionReview.question_id == question_id,
        QuestionReview.checker_type == "generator_plausibility",
    ).order_by(QuestionReview.created_at.asc(), QuestionReview.id.asc()))
    if review is None or not isinstance(review.details, dict):
        return None  # Old verified question: the pure helper uses neutral 50.
    scores = review.details.get("scores")
    return scores if isinstance(scores, dict) else None


def hidden_choices(db: Session, attempt: QuestionAttempt,
                   question: Question) -> tuple[str, ...]:
    """Reconstruct stable ties from persisted attempt UUID and score record."""
    import random
    from star_perks import choose_to_hide

    if not attempt.hints_used:
        return ()
    if attempt.hints_used not in (1, 2):
        raise GameConflict("Invalid hidden answer count")
    return choose_to_hide(
        question.correct_key, question_plausibility(db, question.id),
        count=attempt.hints_used, rng=random.Random(attempt.id.int),
    )


def hide_wrong_options(
    db: Session, *, run_id: UUID, user_id: UUID, question_id: UUID,
    count: int, request_id: UUID,
) -> tuple[QuestionAttempt, Question]:
    """Pay bronze and hide lowest-scored false choices, as one transaction.

    The server persists 0/1/2 hidden count on QuestionAttempt. For a given
    attempt UUID, generated scores and deterministic tie shuffle reproduce
    exactly the same set after restart. One 2-option action costs 3 bronze;
    two separate 1-option actions cost 2+2.
    """
    from persistence.models import WalletTransaction
    from persistence.star_economy import lock_user_stars, spend_star
    from star_perks import PerkUnavailable, remove_price

    operation_key = f"star:{user_id}:{request_id}:hide"
    try:
        lock_user_stars(db, user_id)
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        previous = db.scalar(select(WalletTransaction).where(
            WalletTransaction.idempotency_key == operation_key
        ))
        if previous is not None:
            if (previous.user_id != user_id or previous.run_id != run_id
                    or previous.reason != f"star/bronze/hide-{count}"
                    or previous.amount not in (-2, -3)):
                raise PerkUnavailable("Idempotency key reused")
            cached = pending_question(db, run_id=run.id)
            if cached is None or cached[0].question_id != question_id:
                raise GameConflict("Previous hint already applied to an old question")
            db.commit()
            return cached
        if run.status != "main":
            raise GameConflict("No hints on bonus or finished games")
        cached = pending_question(db, run_id=run.id)
        if cached is None or cached[0].question_id != question_id:
            raise GameConflict("No matching unanswered question")
        attempt, question = cached
        price = remove_price(attempt.hints_used, count)
        # Validate sorting and scores BEFORE spending any stars.
        hidden_choices(db, attempt, question)
        spend_star(db, user_id=user_id, run_id=run_id, tier="bronze",
                   amount=price, action=f"hide-{count}",
                   operation_key=operation_key)
        previous_expenses = dict(attempt.appeal_expenses or {})
        previous_expenses["hint_bronze"] = int(previous_expenses.get("hint_bronze", 0)) + price
        attempt.appeal_expenses = previous_expenses
        attempt.hints_used += count
        run.hints_used += 1  # One action, whether it hides one or two.
        db.commit()
        db.refresh(attempt)
        return attempt, question
    except Exception:
        db.rollback()
        raise
