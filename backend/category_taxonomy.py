"""Shared quiz category taxonomy for helper skills and topic rotation.

"area" is the established 20-skill helper classification; "family" is the
wider editorial group used only to prevent adjacent similar questions.
All identifiers are internal, not user-facing translations or model output.
"""
from __future__ import annotations

from consts import QUESTION_CATEGORIES

CATEGORY_TOPICS = {
    "Ancient history": "History",
    "Medieval history": "History",
    "Modern history": "History",
    "World geography": "Geography",
    "Countries and capitals": "Geography",
    "Cities and landmarks": "Geography",
    "Oceans, rivers, and lakes": "Geography",
    "Mountains and natural landscapes": "Geography",
    "Astronomy and space": "Astronomy",
    "Physics": "Physics",
    "Chemistry": "Chemistry",
    "Biology": "Biology",
    "Human anatomy": "Medicine",
    "Medicine and health": "Medicine",
    "Animals": "Biology",
    "Plants and botany": "Biology",
    "Earth science and geology": "Geography",
    "Weather and climate": "Meteorology",
    "Mathematics": "Mathematics",
    "Computer science": "Technology",
    "Technology": "Technology",
    "Inventions and discoveries": "Technology",
    "Engineering": "Engineering",
    "Literature": "Literature",
    "Poetry": "Literature",
    "Authors and books": "Literature",
    "Languages and linguistics": "Literature",
    "Mythology": "Mythology",
    "Religion and world beliefs": "Mythology",
    "Philosophy": "Culture",
    "Psychology": "Medicine",
    "Visual arts": "Art",
    "Architecture": "Engineering",
    "Classical music": "Music",
    "Popular music": "Music",
    "Musical instruments": "Music",
    "Cinema": "Cinema",
    "Television": "Cinema",
    "Theater": "Art",
    "Comics and graphic novels": "Comics",
    "Video games": "Technology",
    "Board games and puzzles": "Culture",
    "Sports": "Sports",
    "Olympic Games": "Sports",
    "Food and cooking": "Culture",
    "Drinks and culinary traditions": "Culture",
    "Fashion and clothing": "Art",
    "Transportation": "Engineering",
    "Everyday objects and household knowledge": "Culture",
    "Traditions, customs, and world culture": "Culture",
}

# Wide families are intentionally coarser than Platinum Helper competence.
# e.g. "Physics" and "Engineering" are different helper skills but are
# treated as related when scheduling consecutive questions.
_AREA_FAMILIES = {
    "History": "history",
    "Geography": "geography_earth",
    "Meteorology": "geography_earth",
    "Physics": "physical_sciences_technology",
    "Mathematics": "physical_sciences_technology",
    "Chemistry": "physical_sciences_technology",
    "Astronomy": "physical_sciences_technology",
    "Engineering": "physical_sciences_technology",
    "Technology": "physical_sciences_technology",
    "Biology": "life_health",
    "Medicine": "life_health",
    "Literature": "literature",
    "Mythology": "humanities",
    "Art": "arts_media",
    "Music": "arts_media",
    "Cinema": "arts_media",
    "Comics": "arts_media",
    "Sports": "sports_games",
    "Culture": "culture_everyday",
    "Agriculture": "culture_everyday",
}

# A few editorial families are intentionally distinct from the knowledge
# areas assigned to helpers (which must remain unchanged).
_FAMILY_OVERRIDES = {
    "Philosophy": "humanities",
    "Psychology": "humanities",
    "Video games": "sports_games",
    "Board games and puzzles": "sports_games",
}

assert set(CATEGORY_TOPICS) == set(QUESTION_CATEGORIES), (
    "Every question category must have a helper competence area"
)
assert set(CATEGORY_TOPICS.values()) <= set(_AREA_FAMILIES), (
    "Every used helper competence area must have a scheduling family"
)
assert set(_FAMILY_OVERRIDES) <= set(QUESTION_CATEGORIES)


def category_family(category: str) -> str:
    """A stable editorial family for one canonical category name."""
    return _FAMILY_OVERRIDES.get(category, _AREA_FAMILIES[CATEGORY_TOPICS[category]])


def prefer_other_family(categories: list[str], previous: str | None) -> list[str]:
    """Stable partition; only prefer different families, never drop a topic.

    If the allowlist leaves no unrelated category, still return the original
    candidates so a question is always possible. Caller controls reuse and
    deterministic/random selection separately.
    """
    if previous is None:
        return list(categories)
    family = category_family(previous)
    return ([cat for cat in categories if category_family(cat) != family]
            + [cat for cat in categories if category_family(cat) == family])
