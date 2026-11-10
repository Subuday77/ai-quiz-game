"""Conservative source-backed safeguards for tagged critic reviews.

A model verdict is evidence, not authority. These checks use only established,
locally verifiable facts and high-confidence literal leakage; they intentionally
do NOT claim to fact-check arbitrary historical or geographical questions.
Extend evidence with independently verified references and offline tests.
"""
import re
import unicodedata
from dataclasses import dataclass

from local_leak_filter import exact_answer_leak


# Festival de Cannes official palmares:
# https://www.festival-cannes.com/en/retrospective/1975/awards/
# https://www.festival-cannes.com/en/retrospective/1976/awards/
_CANNES_PALME_WINNER_YEARS = {
    "хроника лет огня": 1975,
    "chronique des annees de braise": 1975,
    "chronicle of the years of fire": 1975,
    "таксист": 1976,
    "taxi driver": 1976,
}
_CANNES_SHARED_WORDING = re.compile(
    r"разделив|совместно|вместе с|shared (?:the )?(?:award|prize)|"
    r"joint(?:ly| winner)|co.winner", re.IGNORECASE
)
# Named rail cities along the Trans-Siberian railway (degrees east).
# This is a narrow reliable example, NOT a general-purpose geocoder.
_ROUTE_LONGITUDE = {
    "москва": 37.62, "moscow": 37.62,
    "екатеринбург": 60.61, "yekaterinburg": 60.61,
    "новосибирск": 82.92, "novosibirsk": 82.92,
    "владивосток": 131.89, "vladivostok": 131.89,
}
_TRANS_SIB = ("транссибир", "trans-siberian", "transsiberian")
_WEST = ("западн", "western")


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip(" «»\"'.?!,;:")


@dataclass(frozen=True)
class SafeguardFindings:
    false_premise: bool = False
    literal_answer_leak: bool = False
    minimum_distractor_penalty: int = 0

    @property
    def codes(self) -> list[str]:
        found = []
        if self.false_premise:
            found.append("source_backed_false_premise")
        if self.literal_answer_leak:
            found.append("literal_answer_leak")
        if self.minimum_distractor_penalty:
            found.append("source_backed_distractor_calibration")
        return found


def _known_false_cannes_co_winner(stem: str) -> bool:
    text = _fold(stem)
    if not (("пальмов" in text) or ("palme d" in text) or ("palme dor" in text)):
        return False
    if not _CANNES_SHARED_WORDING.search(text):
        return False
    years = {int(year) for year in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)}
    if len(years) != 1:
        return False  # explicit multiple years: do not infer a shared award
    (claimed_year,) = years
    for title, actual_year in _CANNES_PALME_WINNER_YEARS.items():
        if title in text and claimed_year != actual_year:
            return True
    return False


def _obvious_rail_endpoint_elimination(question, difficulty: int) -> int:
    stem = _fold(question.question)
    if difficulty < 6 or not any(term in stem for term in _TRANS_SIB):
        return 0
    if not any(term in stem for term in _WEST):
        return 0
    # At most one clearly identifiable western terminus. This only handles
    # fully known city options on the named route; unknown = no penalty.
    city_keys = [_fold(answer) for answer in question.answers.values()]
    if len(city_keys) != 4 or len(set(city_keys)) != 4:
        return 0
    if any(city not in _ROUTE_LONGITUDE for city in city_keys):
        return 0
    distances = [_ROUTE_LONGITUDE[city] for city in city_keys]
    correct_city = _fold(question.answers[question.correct_answer])
    if correct_city not in ("москва", "moscow"):
        return 0
    if _ROUTE_LONGITUDE[correct_city] != min(distances):
        return 0
    if not all(x >= _ROUTE_LONGITUDE[correct_city] + 15 for x in distances
               if x != _ROUTE_LONGITUDE[correct_city]):
        return 0
    return 2


def find_safeguards(question, *, requested_difficulty: int) -> SafeguardFindings:
    return SafeguardFindings(
        false_premise=_known_false_cannes_co_winner(question.question),
        literal_answer_leak=exact_answer_leak(question),
        minimum_distractor_penalty=_obvious_rail_endpoint_elimination(
            question, requested_difficulty),
    )
