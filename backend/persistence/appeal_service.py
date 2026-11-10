"""Transactional appeal submission, state changes and settlement.

The public routes may file and read appeals; only a trusted internal judge
worker may call settle_appeal(). No API lets a player decide a verdict.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.appeal_policy import (
    appeal_deposit_tier, appeal_fee_bronze, bonus_wager_refund,
    compensation_tier, loss_compensation_allowed, month_bounds,
    permanent_block_reason, refund_deposit, should_auto_uphold,
    validate_complaint,
)
from persistence.gameplay import GameConflict, GameNotFound, owned_game
from persistence.models import (
    GameRun, Question, QuestionAppeal, QuestionAttempt, QuestionModeration,
    PlatinumHelperCall, PlatinumHelperPurchase, WalletTransaction,
)
from persistence.star_economy import lock_user_stars, spend_star, star_balance
from persistence.appeal_logging import emit
from persistence.appeal_qa_quota import allowance
from star_perks import PerkUnavailable


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _eligible_attempt(db: Session, *, run: GameRun, question_id: UUID
                      ) -> tuple[QuestionAttempt, Question, bool]:
    attempt = db.scalar(select(QuestionAttempt).where(
        QuestionAttempt.run_id == run.id,
        QuestionAttempt.question_id == question_id,
        QuestionAttempt.superseded_at.is_(None),
    ).order_by(QuestionAttempt.shown_at.desc()).limit(1))
    if attempt is None:
        raise GameConflict("Question not present in this round")
    if attempt.answered_at is None:
        if run.status not in ("main", "bonus"):
            raise GameConflict("Round is no longer active")
        return attempt, db.get(Question, question_id), True
    if attempt.is_correct is False and run.status == "lost":
        return attempt, db.get(Question, question_id), False
    raise GameConflict("Appeal is allowed before answering or after an incorrect loss")


def _filings_this_month(db: Session, *, user_id: UUID, now: datetime) -> int:
    start, end = month_bounds(now)
    return int(db.scalar(select(func.count(QuestionAppeal.id)).where(
        QuestionAppeal.user_id == user_id,
        QuestionAppeal.submitted_at >= start,
        QuestionAppeal.submitted_at < end,
    )) or 0)


def quote_appeal(db: Session, *, run_id: UUID, user_id: UUID,
                 question_id: UUID) -> dict:
    run = owned_game(db, run_id=run_id, user_id=user_id)
    attempt, _, before = _eligible_attempt(
        db, run=run, question_id=question_id)
    old = db.scalar(select(QuestionAppeal.id).where(
        QuestionAppeal.user_id == user_id,
        QuestionAppeal.question_id == question_id,
    ))
    if old is not None:
        raise GameConflict("Question already appealed")
    now = _now()
    count = _filings_this_month(db, user_id=user_id, now=now)
    quota = allowance(db, user_id=user_id, now=now, submitted=count)
    return {
        "question_id": str(question_id),
        "number": attempt.number,
        "before_answer": before,
        "deposit_tier": appeal_deposit_tier(attempt.number),
        "deposit_amount": 1,
        **quota,
        "filing_fee_never_refunded": True,
    }


def _credit(db: Session, *, appeal: QuestionAppeal,
            tier: str, action: str, amount: int = 1) -> None:
    """Ledger-credit exactly once per appeal and purpose."""
    key = f"appeal:{appeal.id}:{action}"
    existing = db.scalar(select(WalletTransaction.id).where(
        WalletTransaction.idempotency_key == key))
    emit("DEBUG", "appeal_wallet_credit_checked", appeal_id=str(appeal.id),
         stage=action, deposit_tier=tier, deposit_amount=amount,
         already_applied=existing is not None)
    if existing is None:
        db.add(WalletTransaction(
            user_id=appeal.user_id, run_id=appeal.run_id,
            idempotency_key=key, amount=amount,
            reason=f"star/{tier}/{action}",
        ))
        db.flush()


def _moderation(db: Session, *, question_id: UUID) -> QuestionModeration:
    moderation = db.get(QuestionModeration, question_id, with_for_update=True)
    if moderation is None:
        moderation = QuestionModeration(question_id=question_id)
        db.add(moderation)
        db.flush()
    return moderation


def file_appeal(db: Session, *, user_id: UUID, run_id: UUID,
                question_id: UUID, request_id: UUID, reasons: list[str],
                comment: str, source_links: list[str],
                confirm_nonrefundable_fee: bool,
                expected_fee_bronze: int | None = None) -> QuestionAppeal:
    """Create one complaint and its two separate possible debits atomically."""
    try:
        lock_user_stars(db, user_id)
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        key = f"appeal:{user_id}:{request_id}"
        previous = db.scalar(select(QuestionAppeal).where(
            QuestionAppeal.idempotency_key == key))
        if previous is not None:
            if (previous.user_id != user_id or previous.run_id != run_id
                    or previous.question_id != question_id
                    or previous.reasons != reasons
                    or previous.comment != comment.strip()
                    or previous.source_links != source_links):
                raise GameConflict("Appeal request ID reused with different contents")
            db.commit()
            return previous

        attempt, question, before = _eligible_attempt(
            db, run=run, question_id=question_id)
        if question is None:
            raise GameConflict("Question missing")
        validated_reasons, cleaned_comment = validate_complaint(
            reasons, comment, before_answer=before)
        if db.scalar(select(QuestionAppeal.id).where(
            QuestionAppeal.user_id == user_id,
            QuestionAppeal.question_id == question_id,
        )) is not None:
            raise GameConflict("Question already appealed")

        # Serialize complaints by different players on the SAME question too.
        db.scalar(select(Question.id).where(
            Question.id == question_id).with_for_update())
        moderation = _moderation(db, question_id=question_id)
        now = _now()
        count = _filings_this_month(db, user_id=user_id, now=now)
        fee = allowance(db, user_id=user_id, now=now,
                        submitted=count)["filing_fee_bronze"]
        deposit_tier = appeal_deposit_tier(attempt.number)
        if expected_fee_bronze is not None and fee != expected_fee_bronze:
            raise GameConflict("Filing fee changed; review the latest quote")
        if fee and not confirm_nonrefundable_fee:
            raise GameConflict("Explicit consent required for nonrefundable fee")
        needed_bronze = fee + (1 if deposit_tier == "bronze" else 0)
        if (needed_bronze and star_balance(db, user_id, "bronze") < needed_bronze
                or deposit_tier != "bronze"
                and star_balance(db, user_id, deposit_tier) < 1):
            raise PerkUnavailable("Insufficient stars for the appeal")

        # Capture small non-identifying player-history counts for Keddy.
        prior_appeals = int(db.scalar(select(func.count(QuestionAppeal.id)).where(
            QuestionAppeal.user_id == user_id)) or 0)
        completed_games = int(db.scalar(select(func.count(GameRun.id)).where(
            GameRun.user_id == user_id,
            GameRun.status.in_(("lost", "completed")))) or 0)

        # Preserve ALL four original options, plus explicit hidden keys. The
        # immutable snapshot lets the player review the exact hint state.
        answers = {key: question.choices[key] for key in "ABCD"}
        from persistence.gameplay import hidden_choices
        hidden_keys = list(hidden_choices(db, attempt, question))
        helper = db.scalar(select(PlatinumHelperPurchase).where(
            PlatinumHelperPurchase.run_id == run.id))
        helper_call_on_question = bool(helper is not None and db.scalar(
            select(PlatinumHelperCall.id).where(
                PlatinumHelperCall.purchase_id == helper.id,
                PlatinumHelperCall.question_attempt_id == attempt.id,
                PlatinumHelperCall.status == "completed",
            ).limit(1)) is not None)
        lost_attempt = (db.scalar(select(QuestionAttempt).where(
            QuestionAttempt.run_id == run.id,
            QuestionAttempt.is_correct.is_(False),
            QuestionAttempt.answered_at.is_not(None),
        ).order_by(QuestionAttempt.answered_at.desc()).limit(1))
            if run.status == "lost" else None)
        snapshot = {
            "question_id": str(question.id), "number": attempt.number,
            "difficulty": attempt.requested_difficulty,
            "question": question.text, "answers": answers,
            "hidden_keys": hidden_keys, "language": run.language,
            "shown_at": attempt.shown_at.isoformat() if attempt.shown_at else None,
        }
        appeal = QuestionAppeal(
            id=uuid4(), user_id=user_id, run_id=run_id,
            attempt_id=attempt.id, question_id=question_id,
            idempotency_key=key, question_snapshot=snapshot,
            language=run.language, reasons=list(validated_reasons),
            comment=cleaned_comment, source_links=source_links,
            context={
                "selected_key": attempt.selected_key,
                "correct_key": question.correct_key,  # private judge input
                "hints_used": attempt.hints_used,
                "expenses": dict(attempt.appeal_expenses or {}),
                "helper_purchase_id": str(helper.id) if helper else None,
                "helper_used_on_question": helper_call_on_question,
                "bonus_wager_accepted": run.bonus_wager_accepted,
                "run_status_at_submission": run.status,
                "lost_question_id": str(lost_attempt.question_id) if lost_attempt else None,
                "prior_appeals_count": prior_appeals,
                "completed_games_count": completed_games,
            },
            before_answer=before, question_number=attempt.number,
            deposit_tier=deposit_tier, filing_fee_bronze=fee,
            submitted_at=now, status="pending",
        )
        db.add(appeal)
        db.flush()
        spend_star(db, user_id=user_id, run_id=run_id,
                   tier=deposit_tier, amount=1,
                   action="appeal-deposit",
                   operation_key=f"appeal:{appeal.id}:deposit")
        db.flush()
        if fee:
            spend_star(db, user_id=user_id, run_id=run_id,
                       tier="bronze", amount=fee,
                       action="appeal-fee",
                       operation_key=f"appeal:{appeal.id}:fee")
            db.flush()

        moderation.distinct_complainants += 1
        reason = permanent_block_reason(
            distinct_players=moderation.distinct_complainants,
            upheld=moderation.upheld_count)
        if reason is not None:
            moderation.permanently_blocked = True
            moderation.block_reason = moderation.block_reason or reason
            question.status = "rejected"
        else:
            question.status = "quarantined"

        # The current question can no longer be answered or paid for twice.
        # GET /question generates another attempt of the same difficulty;
        # offer_question must increment its revision for the same slot.
        if before:
            attempt.superseded_at = now
        if should_auto_uphold(moderation.upheld_count):
            settle_appeal(db, appeal=appeal, decision="upheld",
                          verdict_en="Already upheld twice independently.",
                          verdict_localized="Appeal upheld automatically.",
                          verdict_data={"automated": True})
        db.commit()
        if appeal.status == "upheld":
            emit("INFO", "appeal_settled", appeal_id=str(appeal.id),
                 question_id=str(appeal.question_id), decision=appeal.status,
                 refund_deposit=appeal.deposit_refunded_at is not None)
        return appeal
    except Exception:
        db.rollback()
        raise


def settle_appeal(db: Session, *, appeal: QuestionAppeal, decision: str,
                  verdict_en: str | None = None,
                  verdict_localized: str | None = None,
                  verdict_data: dict | None = None) -> QuestionAppeal:
    """Called by the trusted judge worker within its DB transaction.

    A content-level inconclusive verdict should pass decision='upheld'.
    Technical errors are retried and are NOT considered an upheld appeal.
    """
    if appeal.status in ("upheld", "rejected", "technical_unresolved"):
        return appeal
    if decision not in ("upheld", "rejected", "technical_unresolved"):
        raise ValueError("Unknown appeal verdict")
    # Match filing lock order (user -> question -> moderation) to avoid
    # deadlocks between new complaints and parallel verdict settlement.
    question = db.get(Question, appeal.question_id, with_for_update=True)
    moderation = _moderation(db, question_id=appeal.question_id)
    now = _now()
    appeal.status = decision
    appeal.closed_at = now
    appeal.verdict_en = verdict_en
    appeal.verdict_localized = verdict_localized
    appeal.verdict_data = verdict_data
    if decision == "upheld":
        moderation.upheld_count += 1
    elif decision == "rejected":
        moderation.rejected_count += 1

    if refund_deposit(
        upheld=(decision == "upheld"),
        technical_unresolved=(decision == "technical_unresolved"),
    ):
        _credit(db, appeal=appeal, tier=appeal.deposit_tier,
                action="appeal-deposit-refund")
        appeal.deposit_refunded_at = now

    caused_loss = (not appeal.before_answer and
                   appeal.context.get("run_status_at_submission") == "lost"
                   and loss_compensation_allowed(
                       lost_question_id=appeal.context.get("lost_question_id"),
                       appealed_question_id=str(appeal.question_id)))
    if decision == "upheld":
        # Only verified per-attempt expenses, never guessed round-wide debits.
        expenses = appeal.context.get("expenses") or {}
        hint_bronze = int(expenses.get("hint_bronze", 0))
        if hint_bronze > 0:
            _credit(db, appeal=appeal, tier="bronze",
                    action="appeal-hint-refund", amount=hint_bronze)
            result_data = dict(appeal.verdict_data or {})
            result_data["expenses_refunded"] = [{"tier": "bronze", "amount": hint_bronze,
                                                  "reason": "hints"}]
            appeal.verdict_data = result_data
    if decision == "upheld":
        helper_id = (appeal.context or {}).get("helper_purchase_id")
        if helper_id:
            purchase = db.get(PlatinumHelperPurchase, UUID(helper_id),
                              with_for_update=True)
            if purchase and purchase.run_id == appeal.run_id and purchase.user_id == appeal.user_id:
                if appeal.context.get("helper_used_on_question") and not purchase.refunded:
                    # One additional gold compensation per hired helper,
                    # independent of how often the worker or player retries.
                    key = f"helper:{purchase.id}:appeal-gold"
                    if db.scalar(select(WalletTransaction.id).where(
                            WalletTransaction.idempotency_key == key)) is None:
                        db.add(WalletTransaction(
                            user_id=appeal.user_id, run_id=appeal.run_id,
                            idempotency_key=key, amount=1,
                            reason="star/gold/helper-appeal-compensation"))
                        data = dict(appeal.verdict_data or {})
                        data.setdefault("expenses_refunded", []).append(
                            {"tier": "gold", "amount": 1, "reason": "helper"})
                        appeal.verdict_data = data
                elif purchase.consultations_used == 0 and not purchase.refunded:
                    # Never refund a helper that was subsequently used, or
                    # one already refunded for a successful Q15 run.
                    key = f"helper:{purchase.id}:refund"
                    if db.scalar(select(WalletTransaction.id).where(
                            WalletTransaction.idempotency_key == key)) is None:
                        db.add(WalletTransaction(
                            user_id=appeal.user_id, run_id=appeal.run_id,
                            idempotency_key=key, amount=purchase.price,
                            reason="star/platinum/helper-refund"))
                        purchase.refunded = True
                        data = dict(appeal.verdict_data or {})
                        data.setdefault("expenses_refunded", []).append(
                            {"tier": "platinum", "amount": purchase.price,
                             "reason": "unused_helper"})
                        appeal.verdict_data = data
    if decision == "upheld" and caused_loss:
        _credit(db, appeal=appeal, tier=compensation_tier(appeal.question_number),
                action="appeal-compensation")
        appeal.compensated_at = now
        if bonus_wager_refund(question_number=appeal.question_number,
                              upheld=True, caused_loss=caused_loss and
                              bool(appeal.context.get("bonus_wager_accepted"))):
            _credit(db, appeal=appeal, tier="gold",
                    action="appeal-bonus-wager-refund")
            appeal.bonus_wager_refunded_at = now

    reason = permanent_block_reason(
        distinct_players=moderation.distinct_complainants,
        upheld=moderation.upheld_count)
    if reason is not None:
        moderation.permanently_blocked = True
        moderation.block_reason = moderation.block_reason or reason
        question.status = "rejected"
    elif db.scalar(select(func.count(QuestionAppeal.id)).where(
        QuestionAppeal.question_id == appeal.question_id,
        QuestionAppeal.id != appeal.id,
        QuestionAppeal.status.in_(("pending", "retry_pending")),
    )) == 0:
        question.status = "approved"
    return appeal
