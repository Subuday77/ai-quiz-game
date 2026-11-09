"""Durable, isolated appeal adjudication worker.

Run once from cron (at least daily):
    cd backend && python -m persistence.appeal_worker --once
Or run as a dedicated service:
    cd backend && python -m persistence.appeal_worker --loop

HTTP submission schedules a best-effort immediate run. All state, retry leases,
and wallet settlements are persisted so service restarts cannot grant twice.
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from time import sleep
from uuid import UUID

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

load_dotenv(find_dotenv())
from persistence.database import engine  # noqa: E402
from persistence.models import Question, QuestionAppeal  # noqa: E402
from persistence.star_economy import lock_user_stars  # noqa: E402
from persistence.appeal_service import settle_appeal  # noqa: E402
from persistence.appeal_policy import (MAX_IMMEDIATE_JUDGE_ATTEMPTS,
                                      must_close_technical)  # noqa: E402
from persistence.appeal_ai_security import (keddy_messages, judge_messages,
                                            parse_keddy, parse_judge,
                                            decision_for_policy)  # noqa: E402

LOG = logging.getLogger(__name__)
LEASE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _config(role: str) -> tuple[str, str, int, float]:
    prefix = "QUIZ_COMPLAINT_" + role
    model = os.environ[prefix + "_MODEL_ID"].strip()
    effort = os.getenv(prefix + "_REASONING_EFFORT", "low").strip()
    tokens = int(os.getenv(prefix + "_MAX_OUTPUT_TOKENS", "800"))
    timeout = float(os.getenv(prefix + "_TIMEOUT_SECONDS", "12"))
    if not model or effort not in ("none", "minimal", "low", "medium", "high") or not 128 <= tokens <= 10000 or not 1 <= timeout <= 120:
        raise ValueError("Invalid complaint model configuration")
    return model, effort, tokens, timeout


def _completion(messages: list[dict], role: str) -> str:
    model, effort, tokens, timeout = _config(role)
    # Hard model separation even when a deployment changes generator settings.
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"],
                    timeout=timeout, max_retries=0)
    result = client.responses.create(
        model=model, input=messages, reasoning={"effort": effort},
        max_output_tokens=tokens, text={"format": {"type": "json_object"}},
    )
    if result.status != "completed" or not result.output_text:
        raise RuntimeError("Incomplete complaint model response")
    return result.output_text


def _evidence(appeal: QuestionAppeal) -> dict:
    snapshot = appeal.question_snapshot
    context = appeal.context or {}
    return {
        "question": snapshot["question"],
        "answers": {k: snapshot["answers"][k] for k in "ABCD"},
        "reasons": appeal.reasons, "comment": appeal.comment,
        "sources": appeal.source_links, "question_number": appeal.question_number,
        "language": appeal.language,
        "round_context": {
            "hints_used": int(context.get("hints_used") or 0),
            "selected_key": context.get("selected_key"),
            "before_answer": appeal.before_answer,
            "game_status": context.get("run_status_at_submission"),
            "bonus_wager_accepted": bool(context.get("bonus_wager_accepted")),
            "hidden_keys": snapshot.get("hidden_keys", []),
        },
    }


def _finalize(db: Session, appeal_id: UUID, *, decision: str,
              en: str | None = None, localized: str | None = None,
              data: dict | None = None) -> None:
    # Same lock order as all balance-changing game operations: user -> appeal.
    row = db.get(QuestionAppeal, appeal_id)
    if row is None or row.status not in ("pending", "retry_pending"):
        return
    lock_user_stars(db, row.user_id)
    row = db.scalar(select(QuestionAppeal).where(
        QuestionAppeal.id == appeal_id).with_for_update())
    if row.status not in ("pending", "retry_pending"):
        db.commit()
        return
    settle_appeal(db, appeal=row, decision=decision,
                  verdict_en=en, verdict_localized=localized, verdict_data=data)
    db.commit()


def process_appeal(appeal_id: UUID) -> str:
    """Claim a due appeal and attempt up to 3 model evaluations."""
    SessionFactory = sessionmaker(bind=engine(), expire_on_commit=False)
    with SessionFactory() as db:
        row = db.scalar(select(QuestionAppeal).where(
            QuestionAppeal.id == appeal_id).with_for_update(skip_locked=True))
        if row is None:
            return "not_found_or_leased"
        if row.status not in ("pending", "retry_pending"):
            return row.status
        now = _now()
        if row.next_retry_at and _aware(row.next_retry_at) > now:
            return "not_due"
        if must_close_technical(_aware(row.submitted_at), now):
            db.rollback()
            _finalize(db, appeal_id, decision="technical_unresolved",
                      en="Unable to complete review within seven days.",
                      localized="Review could not be completed for technical reasons.")
            return "technical_unresolved"
        question = db.get(Question, row.question_id)
        if question is None:
            db.rollback()
            return "question_missing"
        # Never let the generator of this question adjudicate its own result.
        judge_model = _config("JUDGE")[0]
        if question.generator_model == judge_model:
            LOG.error("Appeal judge equals generator for question %s", row.question_id)
            row.status = "retry_pending"
            row.next_retry_at = now + timedelta(days=1)
            db.commit()
            return "model_conflict"
        # Commit the lease BEFORE network IO.
        row.status = "retry_pending"
        row.next_retry_at = now + LEASE
        db.commit()

    for _ in range(MAX_IMMEDIATE_JUDGE_ATTEMPTS):
        try:
            with SessionFactory() as db:
                appeal = db.get(QuestionAppeal, appeal_id)
                if appeal is None or appeal.status != "retry_pending":
                    return "already_settled"
                evidence = _evidence(appeal)
                answer_key = appeal.context["correct_key"]
            keddy = parse_keddy(_completion(keddy_messages(evidence), "CADDY"))
            verdict = parse_judge(_completion(judge_messages(
                evidence, correct_key=answer_key, preliminary=keddy,
            ), "JUDGE"))
            with SessionFactory() as db:
                _finalize(
                    db, appeal_id, decision=decision_for_policy(verdict),
                    en=verdict.explanation_en,
                    localized=verdict.explanation_player_language,
                    data={"judge_decision": verdict.decision,
                          "judge_model": _config("JUDGE")[0],
                          "keddy_model": _config("CADDY")[0],
                          "keddy_summary": keddy.summary_en,
                          "rationale_facts": verdict.rationale_facts},
                )
            return "settled"
        except Exception as exc:
            # No player-controlled text, answers, or tokens in logs.
            LOG.warning("Appeal adjudication attempt failed id=%s type=%s",
                        appeal_id, type(exc).__name__)
            with SessionFactory() as db:
                row = db.scalar(select(QuestionAppeal).where(
                    QuestionAppeal.id == appeal_id).with_for_update())
                if row is None or row.status != "retry_pending":
                    return "already_settled"
                row.judge_attempts += 1
                db.commit()

    with SessionFactory() as db:
        row = db.scalar(select(QuestionAppeal).where(
            QuestionAppeal.id == appeal_id).with_for_update())
        if row is None or row.status != "retry_pending":
            return "already_settled"
        if must_close_technical(_aware(row.submitted_at), _now()):
            db.rollback()
            _finalize(db, appeal_id, decision="technical_unresolved",
                      en="Unable to complete review within seven days.",
                      localized="Review could not be completed for technical reasons.")
            return "technical_unresolved"
        row.next_retry_at = _now() + timedelta(days=1)
        db.commit()
    return "retry_pending"


def process_due(*, limit: int = 50) -> dict[str, int]:
    SessionFactory = sessionmaker(bind=engine(), expire_on_commit=False)
    with SessionFactory() as db:
        now = _now()
        ids = list(db.scalars(select(QuestionAppeal.id).where(
            QuestionAppeal.status.in_(("pending", "retry_pending")),
            (QuestionAppeal.next_retry_at.is_(None) |
             (QuestionAppeal.next_retry_at <= now)),
        ).order_by(QuestionAppeal.submitted_at.asc()).limit(limit)))
    counts: dict[str, int] = {}
    for appeal_id in ids:
        status = process_appeal(appeal_id)
        counts[status] = counts.get(status, 0) + 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.once:
        print(process_due())
    else:
        while True:
            print(process_due())
            sleep(60)
