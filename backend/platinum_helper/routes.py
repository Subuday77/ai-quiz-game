"""Authenticated Platinum Helper API; same-origin and ownership checks."""
import random
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.gameplay import GameConflict, GameNotFound, owned_game
from persistence.models import User
from platinum_helper.core import PROFESSIONAL_SKILLS
from platinum_helper.identity import UI_FAKER_LOCALES, generate_identity
from platinum_helper.fallback_locale import cached_helper_templates
from platinum_helper.responder import HelperUnavailable, talk
from platinum_helper.service import (
    buy, finish_call, get_purchase, card, reserve_call,
)

router = APIRouter(prefix="/api/v2/games", tags=["Platinum Helper"])



class BuyInput(BaseModel):
    profession: str | None = None
    ui_language: str | None = None


class CallInput(BaseModel):
    question_id: UUID
    request_key: str = Field(min_length=8, max_length=80)
    # The CURRENT visible interface language, not GameRun.language.
    # Older clients omit it and use their persisted account preference.
    ui_language: str | None = None


def _own(db: Session, run_id: UUID, user_id: UUID):
    try:
        return owned_game(db, run_id=run_id, user_id=user_id)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None


@router.get("/helper/professions")
def professions(user: User = Depends(authenticated_user)):
    return {"professions": list(PROFESSIONAL_SKILLS)}


@router.get("/{run_id}/helper")
def helper_card(run_id: UUID, user: User = Depends(authenticated_user),
                db: Session = Depends(get_db)):
    _own(db, run_id, user.id)
    purchase = get_purchase(db, run_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="No helper purchased")
    return card(purchase)


@router.post("/{run_id}/helper/purchase", status_code=201)
def purchase(run_id: UUID, payload: BuyInput, request: Request,
             user: User = Depends(authenticated_user),
             db: Session = Depends(get_db)):
    validate_origin(request)
    rng = random.SystemRandom()
    # The browser sends its visible interface language. It is independent of
    # the selected question language, and validated against known Faker locales.
    ui_language = payload.ui_language if payload.ui_language is not None else user.ui_language
    if ui_language not in UI_FAKER_LOCALES:
        raise HTTPException(status_code=422, detail="Unsupported interface language")
    identity = generate_identity(rng, ui_language=ui_language)
    try:
        return buy(db, user_id=user.id, run_id=run_id,
                   profession=payload.profession,
                   name=identity["name"], gender=identity["gender"],
                   nationality=identity["nationality"], rng=rng)
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (ValueError, GameConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/{run_id}/helper/call")
def call_helper(run_id: UUID, payload: CallInput, request: Request,
                user: User = Depends(authenticated_user),
                db: Session = Depends(get_db)):
    validate_origin(request)
    speech_language = (
        payload.ui_language if payload.ui_language is not None
        else user.ui_language
    )
    if speech_language not in UI_FAKER_LOCALES:
        raise HTTPException(status_code=422, detail="Unsupported interface language")
    try:
        old_text, current_card, cached, data = reserve_call(
            db, user_id=user.id, run_id=run_id, question_id=payload.question_id,
            request_key=payload.request_key,
        )
        if cached:
            return {"text": old_text, "helper": current_card}
        # The question and selected option remain in the original game
        # language; only the character's speech follows the visible UI locale.
        data["speech_language"] = speech_language
        try:
            # Only after failed or unsafe model output, look up the
            # already-translated templates from PostgreSQL. No localizer call.
            reply = talk(
                data,
                fallback_loader=lambda: cached_helper_templates(db, speech_language),
            )
        except HelperUnavailable:
            finish_call(db, user_id=user.id, run_id=run_id,
                        call_id=data["call_id"], reply=None)
            raise HTTPException(status_code=503, detail="Helper temporarily unavailable")
        result = finish_call(db, user_id=user.id, run_id=run_id,
                             call_id=data["call_id"], reply=reply)
        if result is None:
            raise HTTPException(status_code=409, detail="Question was already closed")
        return result
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except (ValueError, GameConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
