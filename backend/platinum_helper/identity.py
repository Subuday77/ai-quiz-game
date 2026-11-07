"""Local, token-free generation of persistent fictional helper identities."""
from random import Random

from faker import Faker

# These explicit locale values have well-defined person names; never send the
# generated name through the UI translation service.
PERSON_LOCALES = {
    "en_US": "United States",
    "ru_RU": "Russia",
    "de_DE": "Germany",
    "fr_FR": "France",
    "es_ES": "Spain",
    "it_IT": "Italy",
    "pt_BR": "Brazil",
}


def generate_identity(rng: Random | None = None) -> dict[str, str]:
    rng = rng or Random()
    locale = rng.choice(tuple(PERSON_LOCALES))
    gender = rng.choice(("male", "female"))
    fake = Faker(locale)
    fake.seed_instance(rng.randrange(2**32))
    first = getattr(fake, "first_name_" + gender, fake.first_name)()
    last = getattr(fake, "last_name_" + gender, fake.last_name)()
    # Persist the literal person name, not something translated from a word.
    return {
        "name": f"{first} {last}",
        "gender": gender,
        "nationality": PERSON_LOCALES[locale],
        "locale": locale,
    }
