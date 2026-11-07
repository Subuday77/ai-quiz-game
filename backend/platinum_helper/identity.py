"""Token-free, UI-locale-matched fictional identities for persisted helpers.

Only the selected *interface* language controls Faker. Question language,
profession, and the helper's stored nationality never override it.
"""
from random import Random

from faker import Faker

# Covers every language in frontend/src/app/localization/store.ts. An unknown
# language is rejected rather than silently falling back to Latin en_US.
UI_FAKER_LOCALES = {
    "en": "en_US",
    "de": "de_DE",
    "fr": "fr_FR",
    "es": "es_ES",
    "it": "it_IT",
    "pt": "pt_BR",
    "ru": "ru_RU",
    "he": "he_IL",
    "ar": "ar_SA",
    "ja": "ja_JP",
    "zh": "zh_CN",
}

PERSON_LOCALES = {
    "en_US": "United States",
    "de_DE": "Germany",
    "fr_FR": "France",
    "es_ES": "Spain",
    "it_IT": "Italy",
    "pt_BR": "Brazil",
    "ru_RU": "Russia",
    "he_IL": "Israel",
    "ar_SA": "Saudi Arabia",
    "ja_JP": "Japan",
    "zh_CN": "China",
}


def generate_identity(rng: Random | None = None, *, ui_language: str = "en") -> dict[str, str]:
    """Generate one permanent localized name; the UI never translates it."""
    if ui_language not in UI_FAKER_LOCALES:
        raise ValueError("Unsupported interface language for helper identity")
    rng = rng or Random()
    locale = UI_FAKER_LOCALES[ui_language]
    gender = rng.choice(("male", "female"))
    fake = Faker(locale)
    fake.seed_instance(rng.randrange(2**32))
    first = getattr(fake, "first_name_" + gender, fake.first_name)()
    last = getattr(fake, "last_name_" + gender, fake.last_name)()
    return {
        "name": f"{first} {last}",
        "gender": gender,
        "nationality": PERSON_LOCALES[locale],
        "locale": locale,
    }
