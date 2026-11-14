"""Emergency reuse of globally approved tagged questions, with user-level dedup.

A pooled question is shared by reference through a NEW QuestionAttempt. Never
copy Question rows, publish correct_key, or manufacture a second critic review.
Selection and the final uniqueness check happen under the existing user/run
row locks, matching normal tagged-question persistence.
"""
import os
import random
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from diagnostics import event
from persistence.categories import allowed_categories, available_categories
from persistence.gameplay import (
    GameConflict, GameNotFound, game_state, owned_game, pending_question,
)
from persistence.choice_order import last_choice_order, new_choice_order
from persistence.models import (
    GameRun, Question, QuestionAttempt, QuestionFact, QuestionReport,
    QuestionReview, QuestionTag, QuestionVoteReview, Tag, Topic, User,
)
from persistence.seed_topics import category_slug
from persistence.question_source_policy import (
    admission_probability, community_vote_threshold,
    rating_center, rating_scale,
)
from tagging.contract import Fact, TagCloud, TaggedQuestion
from tagging.core import DuplicateQuestionError, require_unique
from tagging.quality_skill.fact_tool import known_false_fact
from tagging.quality_skill.known_issues import known_qa_issue
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
        candidate = TaggedQuestion(
            difficulty=question.requested_difficulty,
            question=question.text,
            answers=question.choices,
            correct_answer=question.correct_key,
            tags=TagCloud(**tags),
            fact=Fact(subject=fact.subject, relation=fact.relation, object=fact.object),
        )
        # Defense in depth: even before the one-off DB quarantine runs,
        # a previously accepted but now source-disproven fact is never
        # re-issued from the shared pool.
        if known_false_fact(candidate) is not None:
            return None
        known_issue = known_qa_issue(candidate)
        if known_issue is not None:
            event("question_pool_known_issue_skipped", code=known_issue)
            return None
        return candidate
    except ValueError:
        # Historical backfills may contain metadata from an older schema.
        return None


def _eligible_for_bonus_reuse(db: Session, question: Question) -> bool:
    """Fail closed for historical Q16 approvals without Terra v2, no new AI cost.

    A stored v5 critic acceptance alone does NOT satisfy current bonus rules.
    Keep user exposure history intact; reuse only independently reviewed items.
    """
    from pydantic import ValidationError
    from tagging.quality_skill.arbiter import (
        ARBITRATION_CONTRACT_VERSION, ArbitrationVerdict, is_suitable,
        stem_claims_anchored,
    )

    reviews = db.scalars(
        select(QuestionReview).where(
            QuestionReview.question_id == question.id,
            QuestionReview.checker_type == "ai_tagged_critic",
            QuestionReview.decision == "accept",
            QuestionReview.contract_version == "tagged_review_v5",
        ).order_by(QuestionReview.created_at.desc())
    ).all()
    for review in reviews:
        details = review.details if isinstance(review.details, dict) else {}
        primary = details.get("verdict")
        assessment = details.get("assessment")
        arbitration = details.get("arbitration")
        if not (isinstance(primary, dict) and isinstance(assessment, dict)
                and isinstance(arbitration, dict)):
            continue
        if (primary.get("difficulty") == "too_easy"
                or primary.get("tags_valid") != "pass"
                or primary.get("fact_valid") != "pass"
                or primary.get("facts") != "pass"
                or primary.get("options") != "pass"
                or primary.get("leakage") != "pass"
                or primary.get("semantic_duplicate") is not False
                or assessment.get("critical") != "none"):
            continue
        score = assessment.get("total")
        threshold = assessment.get("score_threshold")
        if (not isinstance(score, (int, float)) or isinstance(score, bool)
                or not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or threshold <= 0 or score >= threshold):
            continue
        if (arbitration.get("checker_type") != "ai_terra_arbiter"
                or arbitration.get("provider") != "openai"
                or arbitration.get("contract_version") != ARBITRATION_CONTRACT_VERSION
                or arbitration.get("decision") != "accept"):
            continue
        try:
            verdict = ArbitrationVerdict.model_validate(arbitration["details"])
        except (KeyError, ValidationError, ValueError, TypeError):
            continue
        if (stem_claims_anchored(verdict, question.text)
                and is_suitable(
                    verdict, correct_answer=question.correct_key, bonus=True
                )[0]):
            return True
    return False


def offer_approved_pool_question(
    db: Session, *, run_id: UUID, user_id: UUID,
    category: str, max_candidates: int | None = None,
    expected_bonus_topic_index: int | None = None,
    rating_filter: bool = False,
    replace_question_id: UUID | None = None,
    replace_attempt_id: UUID | None = None,
    replace_manual: bool = False,
    category_operation_key: str | None = None,
) -> tuple[QuestionAttempt, Question] | None:
    """Find an already reviewed question and atomically offer it to this user.

    Only tagged v5 critic-accepted, currently approved, unreported questions
    enter the pool. Bonus reuse additionally requires Terra v2 accepted
    evidence and the new strict difficulty gate. Old reviews cannot bypass it.
    Exact-language/category/difficulty matches are mandatory. Paid manual
    replacement searches only the requested category and spends silver in the
    same transaction as the new attempt, never on a pool miss.
    The SQL anti-join prevents exact repeats; require_unique also prevents
    fact/tag/semantic repeats, including those across the user's languages.
    """
    if max_candidates is None:
        max_candidates = scan_limit()
    if not 1 <= max_candidates <= 1000:
        raise ValueError("Pool scan limit must be in 1..1000")
    checked = 0
    duplicate = 0
    admitted = []
    bonus_unverified = 0
    try:
        # Serialize concurrent assignments, including other game runs owned
        # by the same user. Acquire locks in the same order as offer_question.
        db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game has ended")
        if (run.status == "bonus" and expected_bonus_topic_index is not None
                and run.bonus_topic_index != expected_bonus_topic_index):
            raise GameConflict("Bonus category changed during pool lookup")
        cached = pending_question(db, run_id=run_id)
        replacing = replace_question_id is not None
        if category not in allowed_categories(run):
            if replacing:
                raise GameConflict("Category excluded from this game")
            db.rollback()
            return None
        old_attempt = None
        switch_price = None
        if replacing:
            from star_perks import category_change_price
            if run.status != "main" or not category_operation_key or replace_attempt_id is None:
                raise GameConflict("Invalid paid category replacement")
            if (cached is None or cached[0].id != replace_attempt_id
                    or cached[0].question_id != replace_question_id):
                raise GameConflict("Question changed during category selection")
            old_attempt, old_question = cached
            if category not in available_categories(
                db, run, old_attempt.number, changing=True,
            ):
                raise GameConflict("Category is not available")
            if category == db.get(Topic, old_question.topic_id).labels.get("en"):
                raise GameConflict("New category matches current category")
            switch_price = category_change_price(
                allowed_count=len(allowed_categories(run)),
                changes_used=old_attempt.category_change_count,
                manual_used=old_attempt.manual_category_used,
                manual=replace_manual,
            )
        elif cached is not None:
            db.commit()
            event("question_pool_fallback", outcome="cached_pending")
            return cached
        elif (run.status == "main"
              and category not in available_categories(
                  db, run, game_state(run, db).main_question_index + 1,
              )):
            raise GameConflict("Category already played in this game")

        game_progress = game_state(run, db)
        difficulty = game_progress.current_difficulty
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
        pending_community_review = exists(
            select(QuestionVoteReview.question_id).where(
                QuestionVoteReview.question_id == Question.id,
                QuestionVoteReview.status.in_(("pending", "retry_pending")),
            )
        )
        center = rating_center() if rating_filter else 0.0
        scale = rating_scale() if rating_filter else 1.0
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
                ~pending_community_review,
                Question.working_vote_score > community_vote_threshold(),
            )
            # Candidate order remains random in Model First. In DB First,
            # each verified eligible candidate is admitted independently.
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
            if run.status == "bonus" and not _eligible_for_bonus_reuse(db, saved):
                bonus_unverified += 1
                continue
            candidate = _tagged_from_saved(db, saved)
            if candidate is None:
                continue
            try:
                require_unique(candidate, history)
            except DuplicateQuestionError:
                duplicate += 1
                continue
            if rating_filter:
                probability = admission_probability(
                    saved.working_vote_score, center=center, scale=scale,
                )
                if random.random() >= probability:
                    continue
                admitted.append(saved)
            else:
                admitted = [saved]
                break

        event("db_pool_candidates", count=len(ids), checked=checked,
              passed_quality=len(admitted) if not rating_filter else None)
        if rating_filter:
            event("db_admitted", count=len(admitted), center=center, scale=scale)
        if admitted:
            saved = random.choice(admitted) if rating_filter else admitted[0]
            # The community threshold can be crossed by another player while
            # the pool is scanning. Lock and re-read before assigning the ID.
            saved = db.scalar(select(Question).where(
                Question.id == saved.id,
            ).with_for_update().execution_options(populate_existing=True))
            if (saved is None or saved.status != "approved"
                    or saved.working_vote_score <= community_vote_threshold()
                    or db.scalar(select(QuestionReport.id).where(
                        QuestionReport.question_id == saved.id).limit(1)) is not None
                    or db.scalar(select(QuestionVoteReview.question_id).where(
                        QuestionVoteReview.question_id == saved.id,
                        QuestionVoteReview.status.in_(("pending", "retry_pending")),
                    ).limit(1)) is not None):
                db.rollback()
                event("question_pool_fallback", outcome="withdrawn_during_scan")
                return None
            if replacing:
                from persistence.star_economy import spend_star
                spend_star(
                    db, user_id=user_id, run_id=run.id, tier="silver",
                    amount=switch_price,
                    action="choose-category" if replace_manual else "random-category",
                    operation_key=category_operation_key,
                )
                old_attempt.superseded_at = datetime.now(timezone.utc)
                run.hints_used += 1
            number = (old_attempt.number if replacing else
                      16 if run.status == "bonus" else game_progress.main_question_index + 1)
            revision = (old_attempt.revision + 1 if replacing else
                        int(db.scalar(select(func.coalesce(func.max(QuestionAttempt.revision), -1))
                                      .where(QuestionAttempt.run_id == run.id,
                                             QuestionAttempt.number == number))) + 1)
            attempt = QuestionAttempt(
                run_id=run.id, question_id=saved.id,
                number=number,
                requested_difficulty=difficulty,
                revision=revision,
                category_change_count=old_attempt.category_change_count + 1 if replacing else 0,
                manual_category_used=old_attempt.manual_category_used + (
                    1 if replace_manual else 0
                ) if replacing else 0,
                choice_order=new_choice_order(last_choice_order(db, saved.id)),
            )
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            event("question_pool_fallback", outcome="hit", examined=checked,
                  duplicate_skips=duplicate, unverified_bonus_skips=bonus_unverified,
                  requested_difficulty=difficulty)
            if rating_filter:
                event("db_first_hit", question_id=str(saved.id),
                      requested_difficulty=difficulty)
            return attempt, saved

        db.rollback()
        event("question_pool_fallback",
              outcome="no_admitted" if rating_filter else
              "no_verified_bonus" if bonus_unverified and run.status == "bonus"
              else "no_unique_match",
              examined=checked, duplicate_skips=duplicate,
              unverified_bonus_skips=bonus_unverified,
              requested_difficulty=difficulty)
        return None
    except Exception:
        db.rollback()
        raise
