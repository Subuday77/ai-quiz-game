"""Conservative tagged-output repair and bounded validation retry guidance.

Strict provider JSON Schema does not include portable array length bounds.
Pydantic is authoritative. Exactly ONE metadata-only error may be repaired:
four distinct valid entity slugs where the declared fact.subject is present.
Never change the question, options, correct answer, difficulty or fact triple.
"""
from collections.abc import Mapping

from pydantic import ValidationError

from .contract import SLUG_PATTERN


# Static, non-user-supplied instructions. Never insert raw ValidationError
# messages, candidates, previous user history, or provider responses in them.
_ENTITY_HINT = (
    "The previous question contained invalid entity tags. Return exactly 1 to "
    "3 UNIQUE canonical lowercase English kebab-case tags in tags.entity; "
    "fact.subject MUST appear in tags.entity. Do not repeat the invalid tags."
)
_SCHEMA_HINT = (
    "The previous question failed server-side schema validation. Return exactly "
    "four different answer strings keyed A, B, C, D; a valid correct_answer; "
    "tags.entity 1-3, tags.concept 1-4 and tags.keyword 0-4 unique canonical "
    "English kebab-case slugs; fact.subject must occur in tags.entity. "
    "Use exactly the requested difficulty. Correct the previous invalid structure."
)
_DIFFICULTY_HINT = (
    "The previous question used the wrong difficulty. Return the exact requested "
    "numeric difficulty level in the next question."
)
_JSON_HINT = (
    "The previous response was not valid JSON. Return one complete JSON object "
    "matching the required output contract; no Markdown fences or extra text."
)


def _only_entity_too_long(exc: ValidationError) -> bool:
    errors = exc.errors(include_input=False, include_url=False)
    return len(errors) == 1 and errors[0]["loc"] == ("tags", "entity") and (
        errors[0]["type"] == "too_long"
    )


def repair_four_entity_tags(raw: object, error: ValidationError) -> dict | None:
    """Return a COPY of one safely repaired candidate, otherwise None.

    Four tags are a safe metadata-only repair ONLY when all tags are already
    unique valid slugs, the sole Pydantic failure is list length, and the
    asserted fact subject is among those original four tags. Keep the first
    three unless that would discard fact.subject; then keep first two+subject.
    """
    if not _only_entity_too_long(error) or not isinstance(raw, dict):
        return None
    tags, fact = raw.get("tags"), raw.get("fact")
    if not isinstance(tags, dict) or not isinstance(fact, dict):
        return None
    entities = tags.get("entity")
    subject = fact.get("subject")
    if (not isinstance(entities, list) or len(entities) != 4
            or not isinstance(subject, str) or subject not in entities
            or not SLUG_PATTERN.fullmatch(subject)
            or len(subject) > 140):
        return None
    if any(
        not isinstance(item, str) or len(item) > 140
        or not SLUG_PATTERN.fullmatch(item)
        for item in entities
    ) or len(set(entities)) != 4:
        return None
    kept = entities[:3]
    if subject not in kept:
        kept = [*entities[:2], subject]
    corrected = dict(raw)
    corrected["tags"] = {**tags, "entity": kept}
    return corrected


def validation_retry_hint(exc: Exception) -> str:
    """A fixed instruction derived only from safe exception classes/locations."""
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_input=False, include_url=False)
        if any(
            len(item["loc"]) >= 2 and item["loc"][:2] == ("tags", "entity")
            for item in errors
        ):
            return _ENTITY_HINT
        return _SCHEMA_HINT
    if isinstance(exc, ValueError) and str(exc) == "AI returned the wrong difficulty":
        return _DIFFICULTY_HINT
    return _JSON_HINT


def is_known_retry_feedback(value: object) -> bool:
    """Only allow predefined guidance, never arbitrary prompt text."""
    return isinstance(value, str) and value in (
        _ENTITY_HINT, _SCHEMA_HINT, _DIFFICULTY_HINT, _JSON_HINT,
    )
