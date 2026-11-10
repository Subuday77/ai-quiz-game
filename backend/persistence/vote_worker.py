"""Durable low-score question moderation by independent Caddy and Judge.

A thumbs-down tally is only a triage signal, never proof of bad facts.
This worker does not create player appeals, deduct/refund stars, or alter
completed games. Failed model calls are retried on the existing scheduler.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from persistence.appeal_ai_security import (
    judge_messages, keddy_messages, parse_judge, parse_keddy,
)
from persistence.appeal_worker import _completion, _config
from persistence.database import engine
from persistence.models import (
    Question, QuestionModeration, QuestionReview, QuestionVoteReview,
)

LOG = logging.getLogger(__name__)
LEASE = timedelta(minutes=30)
DEADLINE = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(time: datetime) -> datetime:
    return time if time.tzinfo is not None else time.replace(tzinfo=timezone.utc)


def _evidence(question: Question) -> dict:
    return {
        "question": question.text,
        "answers": {key: question.choices[key] for key in "ABCD"},
        "reasons": [],
        "comment": (
            "Community rating fell below the configured moderation threshold. "
            "This is only a subjective flag, NOT evidence that the question is "
            "wrong. Independently check factual accuracy, key correctness, "
            "ambiguity and unmistakable answer leakage. Do not uphold only "
            "because of negative votes."
        ),
        "sources": [],
        "question_number": question.requested_difficulty + 1,
        "language": question.language,
        "round_context": {"before_answer": True, "hints_used": 0,
                          "selected_key": None},
    }


def process_vote_review(question_id: UUID) -> str:
    """Single-queue-item claim with persisted lease and one model sequence."""
    factory = sessionmaker(bind=engine(), expire_on_commit=False)
    with factory() as db:
        row = db.scalar(select(QuestionVoteReview).where(
            QuestionVoteReview.question_id == question_id
        ).with_for_update(skip_locked=True))
        if row is None:
            return "not_found_or_leased"
        if row.status not in ("pending", "retry_pending"):
            return row.status
        now = _now()
        if row.next_retry_at and _aware(row.next_retry_at) > now:
            return "not_due"
        if now - _aware(row.queued_at) >= DEADLINE:
            row.status = "technical_unresolved"
            row.reviewed_at = now
            db.commit()
            return row.status
        question = db.get(Question, question_id)
        if question is None:
            return "question_missing"
        # Do not adjudicate questions already independently blocked.
        if question.status == "rejected":
            row.status = "upheld"
            row.verdict = "Question previously blocked by independent moderation."
            row.reviewed_at = now
            db.commit()
            return row.status
        model = os.getenv("QUIZ_COMPLAINT_JUDGE_MODEL_ID", "").strip()
        if not model or model == question.generator_model:
            row.status = "retry_pending"
            row.next_retry_at = now + timedelta(days=1)
            row.attempts += 1
            db.commit()
            LOG.warning("Community review postponed: independent Judge unavailable")
            return "model_conflict"
        evidence = _evidence(question)
        correct_key = question.correct_key
        row.status = "retry_pending"
        row.next_retry_at = now + LEASE
        db.commit()

    try:
        caddy = parse_keddy(_completion(keddy_messages(evidence), "CADDY"))
        decision = parse_judge(_completion(judge_messages(
            evidence, correct_key=correct_key, preliminary=caddy,
        ), "JUDGE"))
    except Exception as exc:
        with factory() as db:
            row = db.scalar(select(QuestionVoteReview).where(
                QuestionVoteReview.question_id == question_id
            ).with_for_update())
            if row is None or row.status != "retry_pending":
                return "already_settled"
            row.attempts += 1
            row.next_retry_at = min(_now() + timedelta(days=1),
                                    _aware(row.queued_at) + DEADLINE)
            db.commit()
        LOG.warning("Community review deferred: %s", type(exc).__name__)
        return "retry_pending"

    with factory() as db:
        # The question may have changed due to a separate player appeal.
        question = db.scalar(select(Question).where(
            Question.id == question_id).with_for_update())
        row = db.scalar(select(QuestionVoteReview).where(
            QuestionVoteReview.question_id == question_id
        ).with_for_update())
        if row is None or row.status != "retry_pending":
            db.rollback()
            return "already_settled"
        verdict = decision.decision
        row.status = verdict
        row.verdict = decision.explanation_player_language
        row.reviewed_at = _now()
        row.next_retry_at = None
        if verdict == "upheld" and question.status != "rejected":
            question.status = "rejected"
            moderation = db.get(QuestionModeration, question_id)
            if moderation is None:
                moderation = QuestionModeration(question_id=question_id)
                db.add(moderation)
            moderation.permanently_blocked = True
            moderation.block_reason = moderation.block_reason or "community_judge"
        db.add(QuestionReview(
            question_id=question_id,
            checker_type="community_vote_judge",
            provider="openai",
            model=_config("JUDGE")[0],
            contract_version="community_vote_judge_v1",
            decision=verdict,
            issues=[] if verdict == "rejected" else ["community_feedback"],
            details={
                "score_at_trigger": row.score_at_trigger,
                "rationale_facts": decision.rationale_facts,
                "caddy_summary": caddy.summary_en,
            },
        ))
        db.commit()
        return verdict


def process_due_vote_reviews(*, limit: int = 10) -> dict[str, int]:
    """Restart-safe scheduled scan; no user-originated IDs or wallet mutations."""
    if not 1 <= limit <= 50:
        raise ValueError("Invalid community review batch limit")
    with Session(engine()) as db:
        due = db.scalars(select(QuestionVoteReview.question_id).where(
            QuestionVoteReview.status.in_(("pending", "retry_pending")),
            or_(QuestionVoteReview.next_retry_at.is_(None),
                QuestionVoteReview.next_retry_at <= _now()),
        ).order_by(QuestionVoteReview.queued_at).limit(limit)).all()
        db.rollback()
    result = {}
    for question_id in due:
        state = process_vote_review(question_id)
        result[state] = result.get(state, 0) + 1
    return result
