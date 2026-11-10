"""Read already-localized helper fallback templates from the shared UI cache.

English lives only in localization/en.json. AI Localizer translates five keys
when the normal UI dictionary is prepared; PostgreSQL stores each translation
with its English source hash. No network or AI request occurs here.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from localization.catalog import LANGUAGES, english_dictionary, source_hash
from persistence.models import UiTranslation


class HelperFallbackKey(StrEnum):
    CONFIDENT_1 = "helper.fallback.confident.1"
    CONFIDENT_2 = "helper.fallback.confident.2"
    TENTATIVE_1 = "helper.fallback.tentative.1"
    TENTATIVE_2 = "helper.fallback.tentative.2"
    UNSURE = "helper.fallback.unsure"


FALLBACK_KEYS = tuple(HelperFallbackKey)


class MissingHelperTranslation(RuntimeError):
    """Locale's source-version-matched fallback copy is not yet cached."""


def cached_helper_templates(db: Session, locale_code: str) -> dict[str, str]:
    """Fetch five validated templates; never contact the localizer on failure.

    The Angular UI already waits for a complete translated dictionary before
    switching language, so under normal play all five strings exist in the
    same shared PostgreSQL cache.
    """
    code = locale_code.strip().lower()
    if code not in LANGUAGES:
        raise MissingHelperTranslation("Unsupported helper speech locale")
    english = english_dictionary()
    source = {key.value: english[key.value] for key in FALLBACK_KEYS}
    if code == "en":
        return source

    rows = db.scalars(
        select(UiTranslation).where(
            UiTranslation.locale_code == code,
            UiTranslation.string_key.in_(list(source)),
        )
    ).all()
    existing = {row.string_key: row for row in rows}
    result = {}
    for key, original in source.items():
        row = existing.get(key)
        if (row is None or row.source_hash != source_hash(original)
                or not isinstance(row.text, str) or not row.text.strip()):
            raise MissingHelperTranslation("Helper fallback locale is not cached")
        result[key] = row.text
    return result
