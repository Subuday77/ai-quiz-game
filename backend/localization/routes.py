"""Public UI dictionary and authenticated independent UI-language preference."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from localization.catalog import LANGUAGES
from localization.service import dictionary_response
from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.models import User

router = APIRouter(prefix="/api/localization", tags=["UI Localization"])


class LanguageOption(BaseModel):
    code: str
    native_name: str


class DictionaryOut(BaseModel):
    language_code: str
    dictionary: dict[str, str]
    source_version: str
    fallback: bool
    missing_count: int


class PreferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language_code: str


@router.get("/languages", response_model=list[LanguageOption])
def languages():
    # NEVER send these labels through the translator.
    return [LanguageOption(code=code, native_name=name)
            for code, name in LANGUAGES.items()]


@router.get("/dictionary/{language_code}", response_model=DictionaryOut)
async def dictionary(language_code: str, db: Session = Depends(get_db)):
    code = language_code.strip().lower()
    if code not in LANGUAGES:
        raise HTTPException(status_code=404, detail="Unsupported UI language")
    return await dictionary_response(db, code)


@router.patch("/preference", response_model=PreferenceIn)
def update_preference(payload: PreferenceIn, request: Request,
                      user: User = Depends(authenticated_user),
                      db: Session = Depends(get_db)):
    validate_origin(request)
    code = payload.language_code.lower()
    if code not in LANGUAGES:
        raise HTTPException(status_code=422, detail="Unsupported UI language")
    user.ui_language = code
    db.commit()
    return PreferenceIn(language_code=code)
