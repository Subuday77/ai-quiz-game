"""Authenticated question-appeal API. No client route may decide or settle an appeal."""
from __future__ import annotations

from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.models import GameRun, Question, QuestionAppeal, User
from persistence.gameplay import GameConflict, GameNotFound
from persistence.appeal_service import quote_appeal, file_appeal, _filings_this_month
from persistence.appeal_qa_quota import allowance
from persistence.appeal_policy import appeal_fee_bronze
from star_perks import PerkUnavailable
from persistence.appeal_logging import emit

router = APIRouter(prefix="/api/v2", tags=["Question appeals"])


class AppealInput(BaseModel):
    question_id: UUID
    request_id: UUID
    reasons: list[str] = Field(default_factory=list, max_length=5)
    comment: str = Field(default="", max_length=1000)
    source_links: list[str] = Field(default_factory=list, max_length=5)
    confirm_nonrefundable_fee: bool = False
    expected_fee_bronze: int = Field(ge=0)


def _appeal_display_status(appeal: QuestionAppeal, *, now: datetime | None = None) -> str:
    """Keep internal retry_pending lease separate from a scheduled retry.

    Worker marks an actively processing appeal retry_pending and gives it
    a 30-minute crash-recovery lease. A genuine deferred retry is scheduled
    for a day later. Do not tell players a technical retry is pending merely
    because the first Judge call is underway.
    """
    if appeal.status != "retry_pending":
        return appeal.status
    if appeal.next_retry_at is None:
        return "pending"
    now = now or datetime.now(timezone.utc)
    when = appeal.next_retry_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ("retry_pending" if when - now > timedelta(minutes=30)
            else "pending")


def _public(appeal: QuestionAppeal, db: Session | None = None) -> dict:
    """Allowlist: hide the private answer key until the game is finished."""
    reveal_key = not appeal.before_answer
    if appeal.before_answer and db is not None and appeal.run_id is not None:
        run = db.get(GameRun, appeal.run_id)
        reveal_key = bool(run and run.status in ("lost", "completed", "abandoned"))
    return {
        "id": str(appeal.id), "question_id": str(appeal.question_id),
        "run_id": str(appeal.run_id) if appeal.run_id else None,
        "question_snapshot": appeal.question_snapshot,
        "language": appeal.language, "reasons": appeal.reasons,
        "comment": appeal.comment, "source_links": appeal.source_links,
        "before_answer": appeal.before_answer, "question_number": appeal.question_number,
        "status": appeal.status,
        "display_status": _appeal_display_status(appeal),
        "submitted_at": appeal.submitted_at,
        "closed_at": appeal.closed_at, "verdict": appeal.verdict_localized,
        "deposit": {"tier": appeal.deposit_tier, "amount": 1,
                    "refunded": appeal.deposit_refunded_at is not None},
        "filing_fee_bronze": appeal.filing_fee_bronze,
        "compensation": {"tier": ("platinum" if appeal.question_number == 16 else
                                 appeal.deposit_tier),
                         "amount": 1 if appeal.compensated_at else 0},
        "bonus_wager_refunded": appeal.bonus_wager_refunded_at is not None,
        "expenses_refunded": (appeal.verdict_data or {}).get("expenses_refunded", []),
        "selected_key": (appeal.context or {}).get("selected_key"),
        "answer_key": ((appeal.context or {}).get("correct_key") if reveal_key else None),
    }



@router.get("/games/{run_id}/appeals/lost-question")
def lost_question_for_appeal(run_id: UUID,
                             user: User = Depends(authenticated_user),
                             db: Session = Depends(get_db)):
    """Recover the exact losing question after F5 without replaying the game."""
    from persistence.gameplay import owned_game, hidden_choices
    from persistence.models import GameRun, QuestionAttempt
    try:
        run = owned_game(db, run_id=run_id, user_id=user.id)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    if run.status != "lost":
        raise HTTPException(status_code=409, detail="This round has not been lost")
    attempt = db.scalar(select(QuestionAttempt).where(
        QuestionAttempt.run_id == run_id, QuestionAttempt.is_correct.is_(False),
        QuestionAttempt.answered_at.is_not(None),
        QuestionAttempt.superseded_at.is_(None),
    ).order_by(QuestionAttempt.answered_at.desc()).limit(1))
    if attempt is None:
        raise HTTPException(status_code=404, detail="No losing question")
    if db.scalar(select(QuestionAppeal.id).where(
        QuestionAppeal.user_id == user.id,
        QuestionAppeal.question_id == attempt.question_id,
    )) is not None:
        raise HTTPException(status_code=409, detail="Already appealed")
    question = db.get(Question, attempt.question_id)
    hidden = set(hidden_choices(db, attempt, question))
    return {
        "question_id": str(question.id), "number": attempt.number,
        "difficulty": attempt.requested_difficulty, "question": question.text,
        "answers": {k: question.choices[k] for k in "ABCD" if k not in hidden},
        "bonus": attempt.number == 16, "hidden_count": attempt.hints_used,
    }


@router.get("/games/{run_id}/appeals/{question_id}/quote")
def appeal_quote(run_id: UUID, question_id: UUID,
                 user: User = Depends(authenticated_user),
                 db: Session = Depends(get_db)):
    try:
        quote = quote_appeal(db, run_id=run_id, user_id=user.id, question_id=question_id)
        question = db.get(Question, question_id)
        if question is None:
            raise HTTPException(status_code=404, detail="Question missing")
        # Display four original options; mark hidden choices as metadata, never as a key.
        from persistence.gameplay import hidden_choices
        from persistence.models import QuestionAttempt, GameRun
        attempt = db.scalar(select(QuestionAttempt).where(
            QuestionAttempt.run_id == run_id,
            QuestionAttempt.question_id == question_id,
            QuestionAttempt.superseded_at.is_(None),
        ).order_by(QuestionAttempt.shown_at.desc()).limit(1))
        run = db.get(GameRun, run_id)
        quote["hint_refund_bronze_if_upheld"] = int(
            (attempt.appeal_expenses or {}).get("hint_bronze", 0))
        quote["loss_compensation_tier_if_upheld"] = (
            "platinum" if attempt.number == 16 else
            ("bronze" if attempt.number <= 5 else
             "silver" if attempt.number <= 10 else "gold")
        ) if not quote["before_answer"] else None
        quote["bonus_wager_refund_if_upheld"] = bool(
            not quote["before_answer"] and attempt.number == 16 and
            run.bonus_wager_accepted)
        quote["question_snapshot"] = {
            "question_id": str(question_id), "question": question.text,
            "answers": {k: question.choices[k] for k in "ABCD"},
            "number": attempt.number, "difficulty": attempt.requested_difficulty,
            "language": run.language,
            "hidden_keys": list(hidden_choices(db, attempt, question)),
        }
        return quote
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/games/{run_id}/appeals", status_code=201)
def submit_appeal(run_id: UUID, payload: AppealInput, request: Request,
                  background_tasks: BackgroundTasks,
                  user: User = Depends(authenticated_user),
                  db: Session = Depends(get_db)):
    validate_origin(request)
    try:
        links = [s.strip() for s in payload.source_links]
        if any(not (s.startswith("https://") or s.startswith("http://"))
               or len(s) > 500 for s in links):
            raise ValueError("Only HTTP(S) source links up to 500 characters are allowed")
        appeal = file_appeal(
            db, user_id=user.id, run_id=run_id, question_id=payload.question_id,
            request_id=payload.request_id, reasons=payload.reasons,
            comment=payload.comment, source_links=links,
            confirm_nonrefundable_fee=payload.confirm_nonrefundable_fee,
            expected_fee_bronze=payload.expected_fee_bronze,
        )
        if appeal.status in ("pending", "retry_pending"):
            # Best-effort immediate processing; durable retries use the worker CLI.
            from persistence.appeal_worker import process_appeal
            background_tasks.add_task(process_appeal, appeal.id)
        emit("INFO", "appeal_submitted", appeal_id=str(appeal.id),
             question_id=str(appeal.question_id), status=appeal.status,
             deposit_tier=appeal.deposit_tier, deposit_amount=1,
             filing_fee_bronze=appeal.filing_fee_bronze)
        return _public(appeal, db=db)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (GameConflict, PerkUnavailable, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/appeals/allowance")
def monthly_appeal_allowance(user: User = Depends(authenticated_user),
                             db: Session = Depends(get_db)):
    """Current monthly free count and fee for the NEXT appeal, in UTC."""
    now = datetime.now(timezone.utc)
    count = _filings_this_month(db, user_id=user.id, now=now)
    quota = allowance(db, user_id=user.id, now=now, submitted=count)
    return {
        "free_remaining": quota["free_appeals_remaining"],
        "free_total": quota["free_appeals_total"],
        "refresh_at": quota["free_appeals_refresh_at"],
        "filed_this_month": count,
        "next_filing_fee_bronze": quota["filing_fee_bronze"],
    }


@router.get("/appeals")
def my_appeals(offset: int = Query(default=0, ge=0),
               limit: int = Query(default=100, ge=1, le=100),
               user: User = Depends(authenticated_user),
               db: Session = Depends(get_db)):
    items = db.scalars(select(QuestionAppeal).where(
        QuestionAppeal.user_id == user.id,
    ).order_by(QuestionAppeal.submitted_at.desc(),
               QuestionAppeal.id.desc()).offset(offset).limit(limit)).all()
    return [_public(item, db=db) for item in items]


@router.get("/appeals/{appeal_id}")
def my_appeal(appeal_id: UUID, user: User = Depends(authenticated_user),
              db: Session = Depends(get_db)):
    appeal = db.scalar(select(QuestionAppeal).where(
        QuestionAppeal.id == appeal_id, QuestionAppeal.user_id == user.id,
    ))
    if appeal is None:
        raise HTTPException(status_code=404, detail="Appeal not found")
    return _public(appeal, db=db)
