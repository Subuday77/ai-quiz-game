"""Canonical UI source strings and native language labels.

UI labels are immutable native names in dropdowns and are NOT sent to AI.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

LANGUAGES = {
    "en": "English", "de": "Deutsch", "fr": "Français",
    "es": "Español", "it": "Italiano", "pt": "Português",
    "ru": "Русский", "he": "עברית", "ar": "العربية",
    "ja": "日本語", "zh": "中文",
}
RIGHT_TO_LEFT = {"he", "ar"}


@lru_cache(maxsize=1)
def english_dictionary() -> dict[str, str]:
    with (Path(__file__).with_name("en.json")).open(encoding="utf-8") as handle:
        dictionary = json.load(handle)
    if not dictionary or any(not isinstance(k, str) or not isinstance(v, str)
                             or not v.strip() for k, v in dictionary.items()):
        raise ValueError("Invalid canonical UI dictionary")
    return dictionary


def source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_version(dictionary: dict[str, str]) -> str:
    canonical = json.dumps(dictionary, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return source_hash(canonical)


def missing_strings(dictionary: dict[str, str], cached: dict[str, tuple[str, str]]
                    ) -> dict[str, str]:
    return {
        key: original for key, original in dictionary.items()
        if key not in cached or cached[key][0] != source_hash(original)
    }
