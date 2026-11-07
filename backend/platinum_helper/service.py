"""Transactional Platinum Helper operations. All player actions are server-owned."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from random import Random
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.gameplay import GameConflict, owned_game, pending_question
from persistence.models import (
    GameRun, PlatinumHelperCall, PlatinumHelperPurchase, WalletTransaction,
    Question, QuestionAttempt, StarAward, Topic, User,
)
from persistence.star_economy import lock_user_stars, star_balance
from platinum_helper.core import (
    CATEGORY_TOPICS, PROFESSIONAL_SKILLS, MAX_CONSULTATIONS,
    choose_outcome, generate_profile,
)


def available_platinum(db: Session, user_id: UUID) -> int:
    """Use the existing star-perks ledger, not a parallel balance system."""
    return star_balance(db, user_id, "platinum")

def card(purchase: PlatinumHelperPurchase) -> dict:
    p = purchase.profile
    return {
        "id": str(purchase.id),
        "name": p["name"],
        "profession": purchase.profession,
        "erudition": p["erudition"],
        "strong_topics": list(p["featured_high"]),
        "weak_topics": list(p["featured_low"]),
        "tagline": p.get("tagline", ""),
        "tagline_id": p.get("tagline_id", 1),
        "tagline_high": p.get("tagline_high") or (p["featured_high"][0] if p["featured_high"] else ""),
        "tagline_low": p.get("tagline_low") or (p["featured_low"][0] if p["featured_low"] else ""),
        "gender": p.get("gender", ""),
        "nationality": p.get("nationality", ""),
        "consultations_remaining": MAX_CONSULTATIONS - purchase.consultations_used,
        "consultations_total": MAX_CONSULTATIONS,
        "refunded": purchase.refunded,
        "price": purchase.price,
    }


def get_purchase(db: Session, run_id: UUID) -> PlatinumHelperPurchase | None:
    return db.scalar(select(PlatinumHelperPurchase).where(
        PlatinumHelperPurchase.run_id == run_id
    ))


def buy(db: Session, *, user_id: UUID, run_id: UUID,
        profession: str | None, name: str, tagline: str = "",
        gender: str = "", nationality: str = "",
        rng: Random | None = None) -> dict:
    """Buy once before the FIRST question is issued, with an atomic star debit."""
    try:
        lock_user_stars(db, user_id)
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        prior = get_purchase(db, run_id)
        if prior:
            return card(prior)  # An HTTP retry never charges again.
        issued = db.scalar(select(func.count(QuestionAttempt.id)).where(QuestionAttempt.run_id == run_id)) or 0
        if run.status != "main" or issued:
            raise GameConflict("A helper must be bought before the first question")
        rng = rng or Random()
        if profession is not None and profession not in PROFESSIONAL_SKILLS:
            raise ValueError("Unsupported helper profession")
        price = 2 if profession else 1
        chosen = profession or rng.choice(list(PROFESSIONAL_SKILLS))
        if available_platinum(db, user_id) < price:
            raise GameConflict("Insufficient platinum stars")
        profile = generate_profile(
            chosen, name=name, tagline=tagline,
            gender=gender, nationality=nationality, rng=rng
        )
        purchase = PlatinumHelperPurchase(
            run_id=run_id, user_id=user_id, price=price, profession=chosen,
            profile=asdict(profile), consultations_used=0, refunded=False,
        )
        db.add(purchase)
        db.flush()
        db.add(WalletTransaction(
            user_id=user_id, run_id=run_id,
            idempotency_key=f"helper:{purchase.id}:purchase",
            amount=-price, reason="star/platinum/helper-purchase",
        ))
        db.commit()
        return card(purchase)
    except Exception:
        db.rollback()
        raise


def reserve_call(db: Session, *, user_id: UUID, run_id: UUID,
                 question_id: UUID, request_key: str, rng: Random | None = None):
    """Freeze one call outcome. No provider call or DB lock during model inference."""
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        purchase = get_purchase(db, run_id)
        if purchase is None:
            raise GameConflict("No helper purchased")
        if not 1 <= len(request_key) <= 80:
            raise ValueError("Invalid request key")
        old = db.scalar(select(PlatinumHelperCall).where(
            PlatinumHelperCall.purchase_id == purchase.id,
            PlatinumHelperCall.request_key == request_key
        ))
        if old:
            original_attempt = db.get(QuestionAttempt, old.question_attempt_id)
            if original_attempt is None or original_attempt.question_id != question_id:
                raise GameConflict("Request key is assigned to another question")
            if old.status == "completed":
                result = (old.response_text, card(purchase), True, None)
                db.commit()
                return result
            if (old.status == "pending" and old.created_at.replace(
                    tzinfo=old.created_at.tzinfo or timezone.utc
                ) > datetime.now(timezone.utc) - timedelta(seconds=35)):
                raise GameConflict("Consultation already in progress")
        if run.status != "main" or run.correct_main_answers >= 15:
            raise GameConflict("Helper unavailable in bonus round")
        cached = pending_question(db, run_id=run_id)
        if cached is None or cached[0].question_id != question_id:
            raise GameConflict("No matching active question")
        attempt, question = cached
        if attempt.number > 15:
            raise GameConflict("Helper unavailable in bonus round")
        in_flight = db.scalar(select(func.count(PlatinumHelperCall.id)).where(
            PlatinumHelperCall.purchase_id == purchase.id,
            PlatinumHelperCall.status == "pending",
        )) or 0
        if purchase.consultations_used >= MAX_CONSULTATIONS or (
                purchase.consultations_used + in_flight >= MAX_CONSULTATIONS and old is None):
            raise GameConflict("No helper consultations remaining")
        # Authoritative category comes from the stored question topic, never the client.
        topic = db.get(Topic, question.topic_id)
        broad = CATEGORY_TOPICS.get(topic.labels.get("en", "") if topic else "")
        if broad is None:
            raise GameConflict("No helper competence mapping for the question")
        if old is None:
            rng = rng or Random()
            # Existing purchased characters predate the revised category map.
            # Their original strengths remain fixed; new areas use MEDIUM.
            outcome = choose_outcome(purchase.profile["skills"].get(broad, "MEDIUM"), rng)
            letter = (rng.choice([c for c in "ABCD" if c != question.correct_key])
                      if outcome == "MISTAKEN" else
                      question.correct_key if outcome == "KNOW" else None)
            old = PlatinumHelperCall(
                purchase_id=purchase.id, request_key=request_key,
                question_attempt_id=attempt.id, topic=broad, outcome=outcome,
                selected_key=letter, status="pending",
            )
            db.add(old)
        else:
            old.status = "pending"
            old.created_at = datetime.now(timezone.utc)
        # Persist before releasing the lock so a client retry cannot reroll.
        db.commit()
        return None, card(purchase), False, {
            "call_id": old.id, "request_key": request_key,
            "outcome": old.outcome, "letter": old.selected_key,
            "question_text": question.text, "choices": question.choices,
            "correct_option_text": question.choices[question.correct_key],
            "topic": broad, "profession": purchase.profession,
            "name": purchase.profile["name"], "language": run.language,
        }
    except Exception:
        db.rollback()
        raise


def finish_call(db: Session, *, user_id: UUID, run_id: UUID,
                call_id: UUID, reply: str | None) -> dict | None:
    """Charge only validated replies; all updates under run lock."""
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        purchase = get_purchase(db, run_id)
        if purchase is None:
            raise GameConflict("No helper purchased")
        call = db.scalar(select(PlatinumHelperCall).where(
            PlatinumHelperCall.id == call_id,
            PlatinumHelperCall.purchase_id == purchase.id,
        ))
        if call is None:
            raise GameConflict("Call not found")
        if call.status == "completed":
            return {"text": call.response_text, "helper": card(purchase)}
        if call.status != "pending":
            raise GameConflict("Call no longer pending")
        # A question answered while the model was busy cannot consume a consultation.
        cached = pending_question(db, run_id=run_id)
        if (not reply or run.status != "main" or cached is None
                or cached[0].id != call.question_attempt_id
                or purchase.consultations_used >= MAX_CONSULTATIONS):
            call.status = "failed"
            db.commit()
            return None
        call.status = "completed"
        call.response_text = reply
        purchase.consultations_used += 1
        db.commit()
        return {"text": reply, "helper": card(purchase)}
    except Exception:
        db.rollback()
        raise


def refund_if_earned(db: Session, run: GameRun) -> bool:
    """Called in the transaction finishing Q15; refund independent of Q16."""
    if run.correct_main_answers < 15:
        return False
    purchase = get_purchase(db, run.id)
    if purchase is None or purchase.consultations_used > 0 or purchase.refunded:
        return False
    purchase.refunded = True
    db.add(WalletTransaction(
        user_id=run.user_id, run_id=run.id,
        idempotency_key=f"helper:{purchase.id}:refund",
        amount=purchase.price, reason="star/platinum/helper-refund",
    ))
    return True
