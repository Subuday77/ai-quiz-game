"""Opt-in, server-only Platinum Helper traces (including private answer keys).

Enable with QUIZ_HELPER_DEBUG=true only for local QA. Never return these
details in HTTP payloads or enable the trace in shared production logs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os

from platinum_helper.core import OUTCOME_WEIGHTS, TOPICS, choose_delivery_style

log = logging.getLogger("quiz.helper.debug")
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
log.setLevel(logging.DEBUG)
log.propagate = False


def enabled() -> bool:
    return os.getenv("QUIZ_HELPER_DEBUG", "false").strip().lower() == "true"


def skill_groups(profile: dict) -> dict[str, list[str]]:
    """List all 20 areas, including the MEDIUM fallback for legacy profiles."""
    skills = profile["skills"]
    return {
        level: [topic for topic in TOPICS if skills.get(topic, "MEDIUM") == level]
        for level in ("HIGH", "MEDIUM", "LOW")
    }


def _emit(event: str, **fields) -> None:
    if not enabled():
        return
    log.debug("HELPER_DEBUG: %s", json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }, ensure_ascii=False, default=str))


def purchased(purchase) -> None:
    if not enabled():
        return
    profile = purchase.profile
    _emit(
        "helper_profile_purchased",
        run_id=str(purchase.run_id),
        helper_id=str(purchase.id),
        helper_name=profile["name"],
        profession=purchase.profession,
        price_platinum=purchase.price,
        wallet_debit_key=f"helper:{purchase.id}:purchase",
        erudition=profile["erudition"],
        skills=skill_groups(profile),
    )


def active_round(info) -> None:
    """Read-only restoration trace: explains identity and language on F5/replay.

    A new purchase emits helper_profile_purchased instead; this event proves
    neither an additional helper roll nor a wallet debit took place.
    """
    if not enabled():
        return
    if info is None:
        _emit("helper_active_round_lookup", found=False)
        return
    helper = info.helper
    _emit(
        "helper_active_round_lookup",
        found=True,
        run_id=str(info.game_id),
        run_language=info.language,
        run_phase=info.phase,
        question_issued=info.question_issued,
        helper_id=helper.get("id") if helper else None,
        helper_name=helper.get("name") if helper else None,
        helper_price_platinum=helper.get("price") if helper else None,
        helper_refunded=helper.get("refunded") if helper else None,
        helper_consultations_remaining=helper.get("consultations_remaining") if helper else None,
    )


def reserved(purchase, call, question, category: str, run_id, *,
             reused: bool, competence_source: str = "unknown") -> None:
    if not enabled():
        return
    profile = purchase.profile
    skill = profile["skills"].get(call.topic, "MEDIUM")
    weights = OUTCOME_WEIGHTS[skill]
    selected_correct = (
        call.selected_key == question.correct_key if call.selected_key is not None else None
    )
    _emit(
        "helper_consultation_reserved",
        run_id=str(run_id),
        helper_id=str(purchase.id),
        helper_name=profile["name"],
        profession=purchase.profession,
        erudition=profile["erudition"],
        call_id=str(call.id),
        question_id=str(question.id),
        question=question.text,
        question_category=category,
        competence_area=call.topic,
        competence_source=competence_source,
        competence_level=skill,
        skills=skill_groups(profile),
        outcome_probabilities_percent=dict(zip(("KNOW", "MISTAKEN", "UNSURE"), weights)),
        outcome=call.outcome,
        confidence=(choose_delivery_style(call.id) if call.selected_key is not None else None),
        selected_key=call.selected_key,
        correct_key=question.correct_key,
        selected_answer_correct=selected_correct,
        reused_outcome=reused,
    )


def finished(db, purchase, call, *, reply: str | None, charged: bool) -> None:
    if not enabled():
        return
    # A selected-key comparison is authoritative for the planned suggestion.
    # It does not claim that every word of the natural-language speech is true.
    from persistence.models import Question, QuestionAttempt

    attempt = db.get(QuestionAttempt, call.question_attempt_id)
    question = db.get(Question, attempt.question_id) if attempt else None
    correct_key = question.correct_key if question else None
    selected_correct = (
        (call.selected_key == correct_key)
        if call.selected_key is not None and correct_key is not None else None
    )
    _emit(
        "helper_consultation_finished",
        run_id=str(purchase.run_id),
        helper_id=str(purchase.id),
        call_id=str(call.id),
        question_id=str(question.id) if question else None,
        competence_area=call.topic,
        competence_level=purchase.profile["skills"].get(call.topic, "MEDIUM"),
        outcome=call.outcome,
        confidence=(choose_delivery_style(call.id) if call.selected_key is not None else None),
        selected_key=call.selected_key,
        correct_key=correct_key,
        selected_answer_correct=selected_correct,
        status=call.status,
        charged=charged,
        speech=reply,
    )
