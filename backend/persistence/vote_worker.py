"""Independently adjudicate quarantined low-rating questions.

One durable review per threshold crossing; twice-acquitted questions receive
working-score resets (-5, -10). A third crossing is automatically banned by
the answer transaction and NEVER calls AI. Historical ballots never reset.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, sessionmaker

from diagnostics import event
from persistence.appeal_ai_security import (
    judge_messages, keddy_messages, parse_judge, parse_keddy,
)
from persistence.appeal_worker import _completion, _config
from persistence.database import engine
from persistence.models import (
    Question, QuestionAppeal, QuestionModeration, QuestionReview,
    QuestionVoteReview,
)
from persistence.question_source_policy import (
    first_acquittal_score, second_acquittal_score,
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
            "The community working rating crossed a negative threshold. "
            "This is a subjective triage signal, not evidence of factual error. "
            "Decide independently whether the question itself has an objectively "
            "incorrect answer key, ambiguous options, or a significant answer leak. "
            "Reject the referral if the question is valid despite dislikes."
        ),
        "sources": [],
        "question_number": question.requested_difficulty + 1,
        "language": question.language,
        "round_context": {"before_answer": True, "hints_used": 0,
                          "selected_key": None},
    }


def _latest(db: Session, question_id: UUID, *, lock: bool = False):
    statement = select(QuestionVoteReview).where(
        QuestionVoteReview.question_id == question_id,
    ).order_by(QuestionVoteReview.referral_number.desc()).limit(1)
    if lock:
        statement = statement.with_for_update(skip_locked=True)
    return db.scalar(statement)


def process_vote_review(question_id: UUID, referral_number: int | None = None) -> str:
    """Claim a numbered referral, call Caddy/Judge once, settle idempotently."""
    factory = sessionmaker(bind=engine(), expire_on_commit=False)
    with factory() as db:
        row = _latest(db, question_id, lock=True)
        if row is None:
            return "not_found_or_leased"
        if referral_number is not None and row.referral_number != referral_number:
            return "stale_referral"
        if row.status not in ("pending", "retry_pending"):
            return row.status
        now = _now()
        if row.next_retry_at and _aware(row.next_retry_at) > now:
            return "not_due"
        if now - _aware(row.queued_at) >= DEADLINE:
            row.status = "technical_unresolved"
            row.reviewed_at = now
            row.next_retry_at = None
            db.commit()
            return "technical_unresolved"
        question = db.get(Question, question_id)
        if question is None:
            return "question_missing"
        if question.status == "rejected":
            row.status = "upheld"
            row.verdict = "Question was already permanently banned."
            row.reviewed_at = now
            row.next_retry_at = None
            db.commit()
            return "upheld"
        model = os.getenv("QUIZ_COMPLAINT_JUDGE_MODEL_ID", "").strip()
        if not model or model == question.generator_model:
            row.status = "retry_pending"
            row.next_retry_at = min(now + timedelta(days=1),
                                    _aware(row.queued_at) + DEADLINE)
            row.attempts += 1
            db.commit()
            LOG.warning("Community Judge postponed: model must differ from generator")
            return "model_conflict"
        evidence = _evidence(question)
        correct_key = question.correct_key
        number = row.referral_number
        row.status = "retry_pending"
        row.next_retry_at = now + LEASE
        db.commit()

    try:
        caddy = parse_keddy(_completion(keddy_messages(evidence), "CADDY"))
        verdict = parse_judge(_completion(judge_messages(
            evidence, correct_key=correct_key, preliminary=caddy,
        ), "JUDGE"))
    except Exception as exc:
        with factory() as db:
            row = db.get(QuestionVoteReview, (question_id, number),
                         with_for_update=True)
            if row is None or row.status != "retry_pending":
                return "already_settled"
            row.attempts += 1
            row.next_retry_at = min(_now() + timedelta(days=1),
                                    _aware(row.queued_at) + DEADLINE)
            db.commit()
        LOG.warning("Community review deferred: %s", type(exc).__name__)
        return "retry_pending"

    with factory() as db:
        # Lock question before review, matching the answer/vote write order.
        question = db.get(Question, question_id, with_for_update=True)
        row = db.get(QuestionVoteReview, (question_id, number),
                     with_for_update=True)
        if question is None or row is None or row.status != "retry_pending":
            db.rollback()
            return "already_settled"
        now = _now()
        row.status = verdict.decision
        row.verdict = verdict.explanation_player_language
        row.judge_model = _config("JUDGE")[0]
        row.reviewed_at = now
        row.next_retry_at = None
        if verdict.decision == "upheld":
            question.status = "rejected"
            moderation = db.get(QuestionModeration, question_id)
            if moderation is None:
                moderation = QuestionModeration(question_id=question_id)
                db.add(moderation)
            moderation.permanently_blocked = True
            moderation.block_reason = moderation.block_reason or "community_judge"
            event("judge_ban", question_id=str(question_id), reason="judge_upheld",
                  referral_number=number)
        elif verdict.decision == "rejected":
            # An acquittal resets ONLY the working score. All individual
            # ballots and vote_score historical net remain unchanged.
            question.working_vote_score = (
                first_acquittal_score() if number == 1
                else second_acquittal_score()
            )
            other_pending_appeal = db.scalar(select(QuestionAppeal.id).where(
                QuestionAppeal.question_id == question_id,
                QuestionAppeal.status.in_(("pending", "retry_pending")),
            ).limit(1))
            if question.status == "quarantined" and other_pending_appeal is None:
                question.status = "approved"
        # Inconclusive or technical failure: remain quarantined for operator.
        db.add(QuestionReview(
            question_id=question_id,
            checker_type="community_vote_judge",
            provider="openai",
            model=row.judge_model,
            contract_version="community_vote_judge_v2",
            decision=verdict.decision,
            issues=[] if verdict.decision == "rejected" else ["community_feedback"],
            details={
                "referral_number": number,
                "score_at_trigger": row.score_at_trigger,
                "historical_net": question.vote_score,
                "working_net": question.working_vote_score,
                "rationale_facts": verdict.rationale_facts,
                "caddy_summary": caddy.summary_en,
            },
        ))
        db.commit()
        return verdict.decision


def process_due_vote_reviews(*, limit: int = 10) -> dict[str, int]:
    """Restart-safe scan; numbered durable queue persists between server runs."""
    if not 1 <= limit <= 50:
        raise ValueError("Invalid community review batch limit")
    with Session(engine()) as db:
        rows = db.execute(select(
            QuestionVoteReview.question_id, QuestionVoteReview.referral_number,
        ).where(
            QuestionVoteReview.status.in_(("pending", "retry_pending")),
            or_(QuestionVoteReview.next_retry_at.is_(None),
                QuestionVoteReview.next_retry_at <= _now()),
        ).order_by(QuestionVoteReview.queued_at).limit(limit)).all()
        db.rollback()
    counts = {}
    for question_id, number in rows:
        status = process_vote_review(question_id, number)
        counts[status] = counts.get(status, 0) + 1
    return counts
