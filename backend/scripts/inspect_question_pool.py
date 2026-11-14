"""Read-only explanation of why DB-first returned zero questions for one game.

From the backend PyCharm terminal:
    python -m scripts.inspect_question_pool 875148f4-bfba-582f-b7ae-5c9491dc2d79

Displays cumulative SQL filter counts and the selected Q1 category. Does not
read question texts/answer keys, call AI, lock records or modify PostgreSQL.
Historical counts are only current snapshots, not an exact time-travel replay.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from sqlalchemy import exists, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from persistence.database import engine  # noqa: E402
from persistence.game_routes import _category_for_round  # noqa: E402
from persistence.categories import allowed_categories  # noqa: E402
from persistence.models import (  # noqa: E402
    GameRun, Question, QuestionAttempt, QuestionFact, QuestionReport,
    QuestionReview, QuestionVoteReview, Topic,
)
from persistence.question_source_policy import community_vote_threshold  # noqa: E402
from persistence.seed_topics import category_slug  # noqa: E402


def _utc(value: datetime) -> datetime:
    """Normalize PostgreSQL and SQLite dates for portable comparisons."""
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))


def inspect(db: Session, run_id: UUID, question_number: int,
            at: datetime | None = None) -> dict:
    run = db.get(GameRun, run_id)
    if run is None:
        raise ValueError("Game run not found")
    if not 1 <= question_number <= 16:
        raise ValueError("Question number must be 1..16")

    # IMPORTANT: _category_for_round() consumes answered categories. Calling
    # it AFTER a failed Q1 was eventually issued and answered can return a
    # completely different category. It must NOT be used as the historic Q1
    # pool lookup category. Reconstruct that first slot from the seeded
    # allowed-category shuffle, independent of completed attempts.
    current_category = _category_for_round(
        run_id, question_number,
        run.bonus_topic_index if question_number == 16 else 0,
        db=db,
    )
    selected_allowlist = list(allowed_categories(run))
    initial_q1_category = random.Random(run_id.int).sample(
        selected_allowlist, k=len(selected_allowlist)
    )[0] if question_number == 1 else None
    first_issued_labels = db.scalar(
        select(Topic.labels)
        .join(Question, Topic.id == Question.topic_id)
        .join(QuestionAttempt, Question.id == QuestionAttempt.question_id)
        .where(QuestionAttempt.run_id == run_id,
               QuestionAttempt.number == question_number)
        .order_by(QuestionAttempt.revision.asc(),
                  QuestionAttempt.shown_at.asc())
        .limit(1)
    )
    first_issued_category = (
        first_issued_labels.get("en")
        if isinstance(first_issued_labels, dict) else None
    )
    selected_category = (
        initial_q1_category or first_issued_category or current_category
    )
    category_basis = (
        "pre-issuance seeded Q1 rotation" if initial_q1_category is not None
        else "first issued question" if first_issued_category
        else "current category reconstruction (not historical)"
    )
    difficulty = 15 if question_number == 16 else question_number - 1
    topic_key = category_slug(selected_category)
    cutoff = community_vote_threshold()
    steps = []

    def count(label: str, *filters) -> None:
        statement = select(func.count(Question.id)).select_from(Question)
        if any(item is topic_filter for item in filters):
            statement = statement.join(Topic, Topic.id == Question.topic_id)
        for item in filters:
            statement = statement.where(item)
        steps.append({"stage": label, "count": int(db.scalar(statement) or 0)})

    topic_filter = Topic.slug == topic_key
    basic = [
        Question.requested_difficulty == difficulty,
        Question.language == run.language,
    ]
    accepted_review = exists(
        select(QuestionReview.id).where(
            QuestionReview.question_id == Question.id,
            QuestionReview.checker_type == "ai_tagged_critic",
            QuestionReview.decision == "accept",
            QuestionReview.contract_version == "tagged_review_v5",
        )
    )
    fact_exists = exists(
        select(QuestionFact.question_id).where(
            QuestionFact.question_id == Question.id,
        )
    )
    seen_by_user = exists(
        select(QuestionAttempt.id).join(
            GameRun, GameRun.id == QuestionAttempt.run_id
        ).where(
            QuestionAttempt.question_id == Question.id,
            GameRun.user_id == run.user_id,
        )
    )
    reported = exists(
        select(QuestionReport.id).where(
            QuestionReport.question_id == Question.id,
        )
    )
    pending_vote_review = exists(
        select(QuestionVoteReview.question_id).where(
            QuestionVoteReview.question_id == Question.id,
            QuestionVoteReview.status.in_(("pending", "retry_pending")),
        )
    )
    count("all_questions")
    count("difficulty", basic[0])
    count("difficulty_and_game_language", *basic)
    conditions = [*basic, topic_filter]
    count("same_category", *conditions)
    conditions.append(Question.status == "approved")
    count("approved", *conditions)
    conditions.append(accepted_review)
    count("accepted_tagged_v5_review", *conditions)
    conditions.append(fact_exists)
    count("has_verified_fact_metadata", *conditions)
    conditions.append(~seen_by_user)
    count("not_seen_by_this_user", *conditions)
    conditions.append(~reported)
    count("not_reported", *conditions)
    conditions.append(~pending_vote_review)
    count("not_pending_community_review", *conditions)
    conditions.append(Question.working_vote_score > cutoff)
    count("meets_vote_score_threshold", *conditions)

    # Compare category supply to see whether the issue is just an exact
    # category match rather than an absence of questions at this level.
    other_categories = db.execute(
        select(Topic.labels, func.count(Question.id))
        .select_from(Question)
        .join(Topic, Question.topic_id == Topic.id)
        .where(Question.requested_difficulty == difficulty,
               Question.language == run.language,
               Question.status == "approved")
        .group_by(Topic.id, Topic.labels)
        .order_by(func.count(Question.id).desc())
        .limit(8)
    ).all()
    issued = db.scalar(select(func.count(QuestionAttempt.id)).where(
        QuestionAttempt.run_id == run_id,
    )) or 0

    # Inspect only category/language/difficulty-matching inventory. The
    # CURRENT seen-by-user count may be zero because a question was shown
    # AFTER the failed request. Its immutable shown_at time distinguishes
    # that case, without ever reading question/option content.
    matching = db.scalars(
        select(Question)
        .join(Topic, Topic.id == Question.topic_id)
        .where(*basic, topic_filter)
        .order_by(Question.created_at, Question.id)
    ).all()
    at_utc = at.astimezone(timezone.utc) if at is not None else None
    candidate_audit = []
    for item in matching:
        first_shown = db.scalar(
            select(func.min(QuestionAttempt.shown_at))
            .join(GameRun, GameRun.id == QuestionAttempt.run_id)
            .where(QuestionAttempt.question_id == item.id,
                   GameRun.user_id == run.user_id)
        )
        first_approved = db.scalar(
            select(func.min(QuestionReview.created_at)).where(
                QuestionReview.question_id == item.id,
                QuestionReview.checker_type == "ai_tagged_critic",
                QuestionReview.decision == "accept",
                QuestionReview.contract_version == "tagged_review_v5",
            )
        )
        fact = db.get(QuestionFact, item.id)
        candidate_audit.append({
            "question_id": str(item.id),
            "status_now": item.status,
            "created_at": item.created_at.isoformat(),
            "first_accepted_review_at": (
                first_approved.isoformat() if first_approved else None
            ),
            "fact_metadata_created_at": (
                fact.created_at.isoformat() if fact else None
            ),
            "first_shown_to_this_user_at": (
                first_shown.isoformat() if first_shown else None
            ),
            "existed_at_requested_time": (
                _utc(item.created_at) <= at_utc if at_utc is not None else None
            ),
            "review_existed_at_requested_time": (
                _utc(first_approved) <= at_utc if at_utc is not None
                and first_approved is not None else
                False if at_utc is not None else None
            ),
            "already_shown_to_user_at_requested_time": (
                _utc(first_shown) <= at_utc if at_utc is not None
                and first_shown is not None else
                False if at_utc is not None else None
            ),
        })

    return {
        "run_id": str(run_id),
        "question_number": question_number,
        "requested_difficulty": difficulty,
        "game_language": run.language,
        "initial_q1_category": initial_q1_category,
        "first_issued_category": first_issued_category,
        "selected_category_now": current_category,
        "category_used_for_filter_counts": selected_category,
        "category_basis": category_basis,
        "initial_category_matches_first_issued": (
            initial_q1_category == first_issued_category
            if initial_q1_category is not None and first_issued_category is not None
            else None
        ),
        "run_status_now": run.status,
        "question_attempts_in_run_now": int(issued),
        "vote_threshold_strictly_greater_than": cutoff,
        "inspection_time_utc": at_utc.isoformat() if at_utc else None,
        "matching_candidate_timeline": candidate_audit,
        "cumulative_sql_filter_counts": steps,
        "approved_questions_other_categories": [
            {"category": (labels or {}).get("en", "unknown"),
             "count": int(n)} for labels, n in other_categories
        ],
        "note": (
            "Read-only CURRENT inventory. Q1 counts are now scoped to the "
            "original seeded first category, not the category after Q1 was "
            "answered. The first issued topic is shown separately. If saved "
            "allowlists, moderation, vote scores or pool inventory changed "
            "since the failed request, counts remain a current snapshot, NOT "
            "historical proof. DB First probabilistic admission happens only "
            "AFTER these SQL filters. Candidate timestamps show when rows, "
            "review and user exposure first existed. Current approval, reports "
            "and vote scores cannot be reconstructed from these timestamps."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("--number", type=int, default=1)
    parser.add_argument("--at", help="Original request time, ISO-8601 UTC; example "
                        "2026-11-10T20:32:57Z")
    args = parser.parse_args()
    at = None
    if args.at:
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if at.tzinfo is None:
            parser.error("--at must include UTC Z or a timezone offset")
    with Session(engine()) as db:
        try:
            report = inspect(db, args.run_id, args.number, at=at)
            print(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
