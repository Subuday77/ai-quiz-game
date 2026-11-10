"""Revision-aware shared UI translation cache backed by PostgreSQL.

There is one English source of truth. Only changed/missing keys are sent
to AI Localizer; earlier successful translations are reused. Short cross-
process leases and failure cooldowns prevent accidental paid-call storms.
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from localization.catalog import english_dictionary, missing_strings, source_hash, source_version
from localization.client import LocalizationUnavailable, translate_missing
from persistence.models import UiLocaleSync, UiTranslation

log = logging.getLogger("quiz.localization")
LEASE_SECONDS = 120
RETRY_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cached(db: Session, code: str) -> dict[str, tuple[str, str]]:
    rows = db.scalars(select(UiTranslation).where(UiTranslation.locale_code == code)).all()
    return {row.string_key: (row.source_hash, row.text) for row in rows}


def _render(source: dict[str, str], cached: dict[str, tuple[str, str]]) -> tuple[dict[str, str], int]:
    rendered = {}
    missing = 0
    for key, english in source.items():
        saved = cached.get(key)
        if saved and saved[0] == source_hash(english):
            rendered[key] = saved[1]
        else:
            rendered[key] = english
            missing += 1
    return rendered, missing


def _claim_lease(db: Session, code: str) -> str | None:
    # Make the per-language guard row with a savepoint. Another worker may
    # create it simultaneously; the unique constraint decides who owns it.
    if db.get(UiLocaleSync, code) is None:
        try:
            with db.begin_nested():
                db.add(UiLocaleSync(locale_code=code, updated_at=_now()))
                db.flush()
        except IntegrityError:
            pass  # Another worker won the first-insert race.

    state = db.scalar(
        select(UiLocaleSync).where(UiLocaleSync.locale_code == code).with_for_update()
    )
    if state is None:
        db.rollback()
        return None

    now = _now()
    if ((_aware(state.lease_until) and _aware(state.lease_until) > now)
            or (_aware(state.retry_after) and _aware(state.retry_after) > now)):
        db.rollback()
        return None

    token = secrets.token_hex(16)
    state.lease_token = token
    state.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    state.updated_at = now
    db.commit()
    return token


def _complete(db: Session, code: str, token: str,
              source: dict[str, str], translations: dict[str, str] | None) -> None:
    try:
        state = db.scalar(
            select(UiLocaleSync).where(UiLocaleSync.locale_code == code).with_for_update()
        )
        if state is None or state.lease_token != token:
            db.rollback()
            return  # A newer worker acquired an expired lease.
        now = _now()
        if translations is not None:
            for key, translated in translations.items():
                db.merge(UiTranslation(
                    locale_code=code, string_key=key, source_hash=source_hash(source[key]),
                    text=translated, updated_at=now,
                ))
            state.retry_after = None
        else:
            # Avoid a new paid request on every page reload during an outage.
            state.retry_after = now + timedelta(seconds=RETRY_SECONDS)
        state.lease_until = None
        state.lease_token = None
        state.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise


async def dictionary_response(db: Session, code: str) -> dict:
    source = english_dictionary()
    version = source_version(source)
    if code == "en":
        return {
            "language_code": code, "dictionary": source, "source_version": version,
            "fallback": False, "missing_count": 0,
        }

    current = _cached(db, code)
    missing = missing_strings(source, current)
    if missing and os.environ.get("AI_LOCALIZER_BASE_URL", "").strip():
        token = _claim_lease(db, code)
        if token:
            translated = None
            try:
                translated = await translate_missing(missing, code)
            except LocalizationUnavailable:
                log.warning("UI translation deferred for locale=%s", code)
            except Exception:
                log.exception("Unexpected UI translation error for locale=%s", code)
            _complete(db, code, token, source, translated)
            current = _cached(db, code)

    rendered, remaining = _render(source, current)
    return {
        "language_code": code, "dictionary": rendered,
        "source_version": version, "fallback": remaining > 0,
        "missing_count": remaining,
    }
