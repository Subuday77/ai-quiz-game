"""Durable appeal adjudication engine, invoked by FastAPI automatically.

Submissions schedule immediate processing. The FastAPI lifespan invokes
process_due() periodically for retries, including overdue work on restart.
State and wallet settlements survive reboots in PostgreSQL.

An optional one-shot maintenance command is available to operators:
    cd backend && python -m persistence.appeal_worker --once
No additional permanently running process is required.
"""
from __future__ import annotations

import argparse
import logging
import os
from time import perf_counter
from datetime import datetime, timedelta, timezone
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
                                            decision_for_policy, response_json_schema,
                                            UnsafeAppealPayload)  # noqa: E402
from persistence.appeal_logging import emit, validation_summary  # noqa: E402

LOG = logging.getLogger(__name__)
LEASE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _config(role: str) -> tuple[str, str, int, float]:
    if role not in ("KEDDY", "JUDGE"):
        raise ValueError("Unknown complaint model role")
    prefix = "QUIZ_COMPLAINT_" + role
    # Temporary compatibility for VPS installations created before the rename.
    # New KEDDY values always take precedence, even if both names exist.
    legacy_prefix = "QUIZ_COMPLAINT_CADDY" if role == "KEDDY" else None

    def setting(suffix: str, default: str | None = None) -> str:
        names = [prefix + "_" + suffix]
        if legacy_prefix:
            names.append(legacy_prefix + "_" + suffix)
        for name in names:
            if name in os.environ:
                return os.environ[name].strip()
        if default is None:
            raise KeyError(names[0])
        return default

    model = setting("MODEL_ID")
    effort = setting("REASONING_EFFORT", "low")
    tokens = int(setting("MAX_OUTPUT_TOKENS", "800"))
    timeout = float(setting("TIMEOUT_SECONDS", "12"))
    if not model or effort not in ("none", "minimal", "low", "medium", "high") or not 128 <= tokens <= 10000 or not 1 <= timeout <= 120:
        raise ValueError("Invalid complaint model configuration")
    return model, effort, tokens, timeout

def _completion(messages: list[dict], role: str, *,
                appeal_id: UUID | None = None, attempt: int | None = None) -> str:
    model, effort, tokens, timeout = _config(role)
    # Hard separation: the models receive no database or financial tools.
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"],
                    timeout=timeout, max_retries=0)
    stage = role.lower()
    emit("INFO", stage + "_started", appeal_id=str(appeal_id) if appeal_id else None,
         stage=stage, model=model, attempt=attempt)
    started = perf_counter()
    try:
        result = client.responses.create(
            model=model, input=messages, reasoning={"effort": effort},
            max_output_tokens=tokens,
            text={"format": {"type": "json_schema",
                             "name": "quiz_appeal_" + stage,
                             "strict": True, "schema": response_json_schema(role)}},
        )
        elapsed_ms = round((perf_counter() - started) * 1000)
        usage = getattr(result, "usage", None)
        details = getattr(usage, "output_tokens_details", None) if usage else None
        status = getattr(result, "status", "unknown")
        stop = getattr(getattr(result, "incomplete_details", None), "reason", None)
        emit("INFO", stage + "_completed",
             appeal_id=str(appeal_id) if appeal_id else None, stage=stage,
             model=model, attempt=attempt, elapsed_ms=elapsed_ms,
             response_status=status, stop_reason=stop,
             input_tokens=getattr(usage, "input_tokens", None),
             output_tokens=getattr(usage, "output_tokens", None),
             reasoning_tokens=getattr(details, "reasoning_tokens", None))
        if status != "completed" or not result.output_text:
            raise RuntimeError("Incomplete complaint model response")
        return result.output_text
    except Exception as exc:
        emit("WARNING", stage + "_failed",
             appeal_id=str(appeal_id) if appeal_id else None, stage=stage,
             model=model, attempt=attempt, error_type=type(exc).__name__,
             elapsed_ms=round((perf_counter() - started) * 1000))
        raise


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
            "prior_appeals_count": int(context.get("prior_appeals_count") or 0),
            "completed_games_count": int(context.get("completed_games_count") or 0),
            "hint_bronze_spent": int((context.get("expenses") or {}).get("hint_bronze") or 0),
            "helper_used_on_question": bool(context.get("helper_used_on_question")),
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
    emit("INFO", "appeal_settled", appeal_id=str(row.id),
         question_id=str(row.question_id), decision=decision,
         refund_deposit=row.deposit_refunded_at is not None,
         compensation_tier=("platinum" if row.question_number == 16 else row.deposit_tier),
         compensation_amount=int(row.compensated_at is not None),
         expenses_refunded=len((row.verdict_data or {}).get("expenses_refunded", [])),
         wager_refunded=row.bonus_wager_refunded_at is not None)


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
        judge_model = os.getenv("QUIZ_COMPLAINT_JUDGE_MODEL_ID", "").strip()
        if question.generator_model == judge_model:
            emit("ERROR", "appeal_model_conflict", appeal_id=str(appeal_id),
                 question_id=str(row.question_id), stage="judge", model=judge_model)
            row.status = "retry_pending"
            row.next_retry_at = min(now + timedelta(days=1),
                                    _aware(row.submitted_at) + timedelta(days=7))
            db.commit()
            return "model_conflict"
        # Commit the lease BEFORE network IO.
        row.status = "retry_pending"
        row.next_retry_at = now + LEASE
        db.commit()
        emit("INFO", "appeal_started", appeal_id=str(appeal_id),
             question_id=str(row.question_id))

    for attempt_no in range(1, MAX_IMMEDIATE_JUDGE_ATTEMPTS + 1):
        stage = "evidence"
        try:
            with SessionFactory() as db:
                appeal = db.get(QuestionAppeal, appeal_id)
                if appeal is None or appeal.status != "retry_pending":
                    return "already_settled"
                evidence = _evidence(appeal)
                answer_key = appeal.context["correct_key"]
            stage = "keddy_input"
            messages = keddy_messages(evidence)
            stage = "keddy"
            raw = _completion(messages, "KEDDY", appeal_id=appeal_id, attempt=attempt_no)
            stage = "keddy_validation"
            keddy = parse_keddy(raw)
            stage = "judge_input"
            messages = judge_messages(evidence, correct_key=answer_key,
                                      preliminary=keddy)
            stage = "judge"
            raw = _completion(messages, "JUDGE", appeal_id=appeal_id, attempt=attempt_no)
            stage = "judge_validation"
            verdict = parse_judge(raw)
            emit("INFO", "judge_verdict", appeal_id=str(appeal_id),
                 stage="judge", model=_config("JUDGE")[0],
                 attempt=attempt_no, decision=verdict.decision)
            stage = "settlement"
            with SessionFactory() as db:
                _finalize(
                    db, appeal_id, decision=decision_for_policy(verdict),
                    en=verdict.explanation_en,
                    localized=verdict.explanation_player_language,
                    data={"judge_decision": verdict.decision,
                          "judge_model": _config("JUDGE")[0],
                          "keddy_model": _config("KEDDY")[0],
                          "keddy_summary": keddy.summary_en,
                          "rationale_facts": verdict.rationale_facts},
                )
            return "settled"
        except Exception as exc:
            reason, fields = validation_summary(exc)
            emit("WARNING", "appeal_attempt_failed", appeal_id=str(appeal_id),
                 stage=stage, attempt=attempt_no, error_type=type(exc).__name__)
            if isinstance(exc, UnsafeAppealPayload):
                emit("WARNING", "appeal_security_rejected",
                     appeal_id=str(appeal_id), stage=stage,
                     attempt=attempt_no, error_type=type(exc).__name__)
            emit("DEBUG", "appeal_validation_detail", appeal_id=str(appeal_id),
                 stage=stage, attempt=attempt_no, error_code=reason,
                 validation_fields=fields)
            with SessionFactory() as db:
                row = db.scalar(select(QuestionAppeal).where(
                    QuestionAppeal.id == appeal_id).with_for_update())
                if row is None or row.status != "retry_pending":
                    return "already_settled"
                row.judge_attempts += 1
                db.commit()
            # Retrying unchanged invalid INPUT or inaccessible model credentials
            # cannot repair the payload. A malformed MODEL OUTPUT may improve.
            if stage in ("evidence", "keddy_input", "judge_input") or type(exc).__name__ in (
                    "BadRequestError", "AuthenticationError", "PermissionDeniedError"):
                emit("DEBUG", "appeal_nontransient_error",
                     appeal_id=str(appeal_id), stage=stage,
                     attempt=attempt_no, error_type=type(exc).__name__)
                break

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
        row.next_retry_at = min(_now() + timedelta(days=1),
                                _aware(row.submitted_at) + timedelta(days=7))
        db.commit()
        emit("INFO", "appeal_retry_scheduled", appeal_id=str(appeal_id),
             next_retry_at=row.next_retry_at.isoformat(),
             status=row.status)
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


def retry_now(appeal_id: UUID) -> str:
    """Operator-only bypass of a DAILY defer (never an in-flight 30m lease).

    Changes no wallet data.  Adjudication still uses the same idempotent DB
    settlement and claimed lease as normal scheduled work.
    """
    SessionFactory = sessionmaker(bind=engine(), expire_on_commit=False)
    with SessionFactory() as db:
        row = db.scalar(select(QuestionAppeal).where(
            QuestionAppeal.id == appeal_id).with_for_update())
        if row is None:
            return "not_found"
        if row.status != "retry_pending":
            return row.status
        now = _now()
        if must_close_technical(_aware(row.submitted_at), now):
            # Do not override expiry; standard processing closes it correctly.
            row.next_retry_at = now
        elif row.next_retry_at is not None and (
            _aware(row.next_retry_at) - now < timedelta(hours=1)
        ):
            # A retry in the next hour may be the lease of active model IO.
            return "already_processing_or_soon_due"
        else:
            row.next_retry_at = now
        db.commit()
    emit("INFO", "appeal_manual_retry", appeal_id=str(appeal_id))
    return process_appeal(appeal_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Appeal maintenance and guarded rechecks")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true",
                       help="Process cases currently due (normally automatic)")
    group.add_argument("--retry-now", type=UUID, action="append",
                       metavar="APPEAL_UUID",
                       help="Force an already deferred case now; repeat for multiple IDs")
    args = parser.parse_args()
    if args.retry_now:
        for appeal_id in args.retry_now:
            print(f"{appeal_id}: {retry_now(appeal_id)}")
    else:
        print(process_due())
