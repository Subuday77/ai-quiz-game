"""Emergency reuse of globally approved tagged questions, with user-level dedup.

A pooled question is shared by reference through a NEW QuestionAttempt. Never
copy Question rows, publish correct_key, or manufacture a second critic review.
Selection and the final uniqueness check happen under the existing user/run
row locks, matching normal tagged-question persistence.
"""
import os
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from diagnostics import event
from persistence.gameplay import (
    GameConflict, GameNotFound, game_state, owned_game, pending_question,
)
from persistence.models import (
    GameRun, Question, QuestionAttempt, QuestionFact, QuestionReport,
    QuestionReview, QuestionTag, Tag, Topic, User,
)
from persistence.seed_topics import category_slug
from tagging.contract import Fact, TagCloud, TaggedQuestion
from tagging.core import DuplicateQuestionError, require_unique
from tagging.repository import history_for_user


def enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def scan_limit() -> int:
    value = os.getenv("QUIZ_QUESTION_POOL_SCAN_LIMIT", "100").strip()
    try:
        number = int(value)
    except ValueError:
        raise ValueError("QUIZ_QUESTION_POOL_SCAN_LIMIT must be an integer") from None
    if number < 1 or number > 1000:
        raise ValueError("QUIZ_QUESTION_POOL_SCAN_LIMIT must be in 1..1000")
    return number


def _tagged_from_saved(db: Session, question: Question) -> TaggedQuestion | None:
    """Reconstruct a checked internal candidate; fail closed on partial metadata."""
    fact = db.get(QuestionFact, question.id)
    if fact is None:
        return None
    tags = {"entity": [], "concept": [], "keyword": []}
    for kind, slug in db.execute(
        select(Tag.kind, Tag.slug)
        .join(QuestionTag, QuestionTag.tag_id == Tag.id)
        .where(QuestionTag.question_id == question.id)
    ):
        if kind in tags:
            tags[kind].append(slug.split(":", 1)[-1])
    if not tags["entity"] or not tags["concept"]:
        return None
    try:
        return TaggedQuestion(
            difficulty=question.requested_difficulty,
            question=question.text,
            answers=question.choices,
            correct_answer=question.correct_key,
            tags=TagCloud(**tags),
            fact=Fact(subject=fact.subject, relation=fact.relation, object=fact.object),
        )
    except ValueError:
        # Historical backfills may contain metadata from an older schema.
        return None


def offer_approved_pool_question(
    db: Session, *, run_id: UUID, user_id: UUID,
    category: str, max_candidates: int | None = None,
) -> tuple[QuestionAttempt, Question] | None:
    """Find an already reviewed question and atomically offer it to this user.

    Only tagged v5 critic-accepted, currently approved, unreported questions
    enter the pool. Exact-language/category/difficulty matches are mandatory.
    The SQL anti-join prevents exact repeats; require_unique also prevents
    fact/tag/semantic repeats, including those across the user's languages.
    """
    if max_candidates is None:
        max_candidates = scan_limit()
    if not 1 <= max_candidates <= 1000:
        raise ValueError("Pool scan limit must be in 1..1000")
    checked = 0
    duplicate = 0
    try:
        # Serialize concurrent assignments, including other game runs owned
        # by the same user. Acquire locks in the same order as offer_question.
        db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game has ended")
        cached = pending_question(db, run_id=run_id)
        if cached is not None:
            db.commit()
            event("question_pool_fallback", outcome="cached_pending")
            return cached

        difficulty = game_state(run).current_difficulty
        category_key = category_slug(category)
        review_exists = exists(
            select(QuestionReview.id).where(
                QuestionReview.question_id == Question.id,
                QuestionReview.checker_type == "ai_tagged_critic",
                QuestionReview.decision == "accept",
                QuestionReview.contract_version == "tagged_review_v5",
            )
        )
        fact_exists = exists(
            select(QuestionFact.question_id)
            .where(QuestionFact.question_id == Question.id)
        )
        seen_by_user = exists(
            select(QuestionAttempt.id)
            .join(GameRun, GameRun.id == QuestionAttempt.run_id)
            .where(QuestionAttempt.question_id == Question.id,
                   GameRun.user_id == user_id)
        )
        reported = exists(
            select(QuestionReport.id)
            .where(QuestionReport.question_id == Question.id)
        )
        ids = db.scalars(
            select(Question.id)
            .join(Topic, Topic.id == Question.topic_id)
            .where(
                Question.status == "approved",
                Question.language == run.language,
                Question.requested_difficulty == difficulty,
                Topic.slug == category_key,
                review_exists,
                fact_exists,
                ~seen_by_user,
                ~reported,
            )
            .order_by(func.random())
            .limit(max_candidates)
        ).all()
        if not ids:
            db.rollback()
            event("question_pool_fallback", outcome="empty", examined=0,
                  requested_difficulty=difficulty)
            return None

        # Re-read history AFTER acquiring the user lock: another game run
        # may have just shown the same fact even if it was never answered.
        history = history_for_user(db, user_id)
        for question_id in ids:
            checked += 1
            saved = db.get(Question, question_id)
            if saved is None or saved.status != "approved":
                continue
            candidate = _tagged_from_saved(db, saved)
            if candidate is None:
                continue
            try:
                require_unique(candidate, history)
            except DuplicateQuestionError:
                duplicate += 1
                continue
            attempt = QuestionAttempt(
                run_id=run.id, question_id=saved.id,
                number=run.correct_main_answers + 1,
                requested_difficulty=difficulty,
            )
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            event("question_pool_fallback", outcome="hit", examined=checked,
                  duplicate_skips=duplicate, requested_difficulty=difficulty)
            return attempt, saved

        db.rollback()
        event("question_pool_fallback", outcome="no_unique_match",
              examined=checked, duplicate_skips=duplicate,
              requested_difficulty=difficulty)
        return None
    except Exception:
        db.rollback()
        raise
