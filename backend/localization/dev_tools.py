"""Opt-in, loopback-only development tools for clearing shared UI translations.

Never enable these routes on Render or any public deployment.
A reset only deletes cached translations/leases; it does not modify users,
accounts, gameplay, or the canonical English dictionary.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from localization.catalog import LANGUAGES
from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.models import UiLocaleSync, UiTranslation, User

router = APIRouter(prefix="/api/localization", tags=["UI Localization — local development"])
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def reset_available(request: Request) -> bool:
    return (
        os.getenv("LOCALIZATION_RESET_ENABLED", "false").strip().lower() == "true"
        and request.client is not None
        and request.client.host in LOOPBACK_HOSTS
        and request.url.hostname in LOOPBACK_HOSTS
    )


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language_code: str  # One translated language, or the explicit value 'all'.


class ResetResult(BaseModel):
    language_code: str
    removed_count: int


@router.get("/reset-capability")
def reset_capability(request: Request, _user: User = Depends(authenticated_user)):
    return {"enabled": reset_available(request)}


@router.post("/reset", response_model=ResetResult)
def reset_translations(payload: ResetRequest, request: Request,
                       _user: User = Depends(authenticated_user),
                       db: Session = Depends(get_db)):
    if not reset_available(request):
        raise HTTPException(status_code=404, detail="Unavailable")
    validate_origin(request)
    code = payload.language_code.lower().strip()
    if code != "all" and (code not in LANGUAGES or code == "en"):
        raise HTTPException(status_code=422, detail="Choose a non-English UI language or 'all'")

    # One DB transaction: translation cache and its lease/cooldown are cleared
    # together. A later GET may regenerate those strings at API cost.
    if code == "all":
        removed = db.scalar(select(func.count()).select_from(UiTranslation)) or 0
        db.execute(delete(UiTranslation))
        db.execute(delete(UiLocaleSync))
    else:
        removed = db.scalar(select(func.count()).select_from(UiTranslation).where(
            UiTranslation.locale_code == code,
        )) or 0
        db.execute(delete(UiTranslation).where(UiTranslation.locale_code == code))
        db.execute(delete(UiLocaleSync).where(UiLocaleSync.locale_code == code))
    db.commit()
    return ResetResult(language_code=code, removed_count=removed)
