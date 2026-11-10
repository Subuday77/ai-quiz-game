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
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from sqlalchemy import exists, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from persistence.database import engine  # noqa: E402
from persistence.game_routes import _category_for_round  # noqa: E402
from persistence.models import (  # noqa: E402
    GameRun, Question, QuestionAttempt, QuestionFact, QuestionReport,
    QuestionReview, QuestionVoteReview, Topic,
)
from persistence.question_source_policy import community_vote_threshold  # noqa: E402
from persistence.seed_topics import category_slug  # noqa: E402


def inspect(db: Session, run_id: UUID, question_number: int) -> dict:
    run = db.get(GameRun, run_id)
    if run is None:
        raise ValueError("Game run not found")
    if not 1 <= question_number <= 16:
        raise ValueError("Question number must be 1..16")

    # The category order is deterministic for the current allowlist.
    # For a historical run whose category allowlist/round state changed,
    # this is a current reconstruction, not a guaranteed past category.
    selected_category = _category_for_round(
        run_id, question_number,
        run.bonus_topic_index if question_number == 16 else 0,
        db=db,
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

    return {
        "run_id": str(run_id),
        "question_number": question_number,
        "requested_difficulty": difficulty,
        "game_language": run.language,
        "selected_category_now": selected_category,
        "run_status_now": run.status,
        "question_attempts_in_run_now": int(issued),
        "vote_threshold_strictly_greater_than": cutoff,
        "cumulative_sql_filter_counts": steps,
        "approved_questions_other_categories": [
            {"category": (labels or {}).get("en", "unknown"),
             "count": int(n)} for labels, n in other_categories
        ],
        "note": (
            "Read-only CURRENT inventory. If game state, allowed categories, "
            "moderation or vote scores changed since the failed request, "
            "counts are not the exact historical snapshot. DB First additionally "
            "applies probabilistic admission ONLY after these SQL filters."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("--number", type=int, default=1)
    args = parser.parse_args()
    with Session(engine()) as db:
        try:
            report = inspect(db, args.run_id, args.number)
            print(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
