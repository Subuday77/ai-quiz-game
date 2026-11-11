"""Revision-aware shared UI translation cache backed by PostgreSQL.

There is one English source of truth. Only changed/missing keys are sent
to AI Localizer; earlier successful translations are reused. Short cross-
process leases and failure cooldowns prevent accidental paid-call storms.
"""
import asyncio
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
PARTIAL_RETRY_SECONDS = 15

# Keep each Localizer call small enough for the default 2048 output tokens.
# Bound fan-out and the whole HTTP request below Angular's 55-second timeout.
BATCH_MAX_KEYS = 32
BATCH_MAX_CHARS = 1400
BATCH_CONCURRENCY = 3
TRANSLATION_BUDGET_SECONDS = 40.0


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


def _batches(missing: dict[str, str]) -> list[dict[str, str]]:
    """Partition missing source strings without splitting any placeholder-bearing value."""
    result: list[dict[str, str]] = []
    batch: dict[str, str] = {}
    chars = 0
    for key, value in missing.items():
        if batch and (len(batch) >= BATCH_MAX_KEYS
                      or chars + len(value) > BATCH_MAX_CHARS):
            result.append(batch)
            batch = {}
            chars = 0
        batch[key] = value
        chars += len(value)
    if batch:
        result.append(batch)
    return result


async def _translate_batch(batch: dict[str, str], code: str,
                           limit: asyncio.Semaphore) -> tuple[dict[str, str], dict[str, str] | None]:
    async with limit:
        try:
            translated = await translate_missing(batch, code)
            if not isinstance(translated, dict) or set(translated) != set(batch):
                raise LocalizationUnavailable("Incomplete translation batch")
            return batch, translated
        except Exception as exc:
            # Do not log text, keys, provider payloads or API credentials.
            log.warning("UI translation batch deferred: locale=%s count=%d error=%s",
                        code, len(batch), type(exc).__name__)
            return batch, None


def _save_batch(db: Session, code: str, token: str,
                source: dict[str, str], translated: dict[str, str]) -> bool:
    """Commit a successful batch immediately, retaining the per-locale lease."""
    try:
        state = db.scalar(
            select(UiLocaleSync).where(UiLocaleSync.locale_code == code).with_for_update()
        )
        if state is None or state.lease_token != token:
            db.rollback()
            return False
        now = _now()
        for key, value in translated.items():
            db.merge(UiTranslation(
                locale_code=code, string_key=key,
                source_hash=source_hash(source[key]), text=value, updated_at=now,
            ))
        state.updated_at = now
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def _release_lease(db: Session, code: str, token: str,
                   *, failed: bool, saved: int) -> None:
    try:
        state = db.scalar(
            select(UiLocaleSync).where(UiLocaleSync.locale_code == code).with_for_update()
        )
        if state is None or state.lease_token != token:
            db.rollback()
            return
        now = _now()
        state.retry_after = (
            now + timedelta(seconds=PARTIAL_RETRY_SECONDS if saved else RETRY_SECONDS)
            if failed else None
        )
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
            batches = _batches(missing)
            semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)
            tasks = [asyncio.create_task(_translate_batch(batch, code, semaphore))
                     for batch in batches]
            saved = 0
            failed = False
            try:
                async with asyncio.timeout(TRANSLATION_BUDGET_SECONDS):
                    for finished in asyncio.as_completed(tasks):
                        batch, translated = await finished
                        if translated is None:
                            failed = True
                            continue
                        if not _save_batch(db, code, token, source, translated):
                            failed = True
                            break
                        saved += len(batch)
            except TimeoutError:
                failed = True
                log.warning("UI translation time budget reached: locale=%s saved=%d total=%d",
                            code, saved, len(missing))
            finally:
                # No orphaned paid calls once this HTTP request is over.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # Other requests only see partial DB progress as fallback=true.
                # The browser commits the new language only on missing_count=0.
                _release_lease(db, code, token,
                               failed=failed or saved != len(missing), saved=saved)
                current = _cached(db, code)

    rendered, remaining = _render(source, current)
    return {
        "language_code": code, "dictionary": rendered,
        "source_version": version, "fallback": remaining > 0,
        "missing_count": remaining,
    }
