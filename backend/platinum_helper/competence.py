"""Question-level skill resolution for Platinum Helper.

For newly reviewed tagged questions, the independent critic supplies the
question's real knowledge area in its existing review call; the area is saved
on Question. Historical questions may have NULL competence_area. Derive a
high-confidence area from their server-side canonical fact/concept tags where
possible, otherwise fall back to the old category mapping.

No model calls or free-text language guessing at consultation time.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.models import Question, QuestionFact, QuestionTag, Tag
from platinum_helper.core import CATEGORY_TOPICS, TOPICS

# Only unambiguous *subject-matter* cues. These canonical English slugs are
# stable across German/Russian/English question wording. We deliberately do
# NOT infer domain from a person's name (Faraday, Nobel, Curie, ...): the
# question might instead ask about that person's biography or historical era.
# The original broad game category is a last resort, not additional evidence.
_LEGACY_SUBJECT_CUES: dict[str, tuple[str, ...]] = {
    "Physics": (
        "electromagnet", "magnetic-field", "electrodynamic", "thermodynamic",
        "quantum-mechanic", "relativity", "photoelectric", "gravitational-wave",
        "nuclear-fission", "optical-refraction",
    ),
    "Medicine": (
        "penicillin", "antibiotic", "vaccine", "vaccination", "anesthesia",
        "surgical", "insulin-therapy", "smallpox", "antiseptic",
    ),
    "Chemistry": (
        "periodic-table", "chemical-element", "chemical-reaction",
        "radioactivity", "polymerization", "electrolysis",
    ),
    "Biology": (
        "photosynthesis", "genetic-code", "dna-structure", "evolutionary",
        "natural-selection", "cell-division", "mitochondri",
    ),
    "Astronomy": (
        "exoplanet", "planetary-orbit", "cosmic-background",
        "black-hole", "pulsar", "stellar", "galaxy",
    ),
    "Mathematics": (
        "calculus", "mathematical-proof", "number-theory",
        "prime-number", "geometry-theorem", "algebraic",
    ),
    "Technology": (
        "printing-press", "transistor", "microprocessor",
        "computer-network", "internet-protocol", "telegraph",
    ),
    "Engineering": (
        "steam-engine", "diesel-engine", "suspension-bridge",
        "turbine", "mechanical-engineer",
    ),
    "Agriculture": (
        "crop-rotation", "plant-breeding", "irrigation",
        "agricultural-machinery",
    ),
    "Meteorology": (
        "atmospheric-pressure", "weather-forecast",
        "hurricane", "meteorological",
    ),
}
assert set(_LEGACY_SUBJECT_CUES) <= set(TOPICS)


def infer_legacy_area(subject: str | None, concepts: list[str] | tuple[str, ...] = ()) -> str | None:
    """Return a single high-confidence area, or None if weak/contradictory."""
    # Subject is the *tested fact's* target, unlike other entity tags which
    # can mention distractors. Concepts can corroborate but never vote twice.
    evidence = (subject or "", *concepts)
    matches = {
        area for area, cues in _LEGACY_SUBJECT_CUES.items()
        if any(cue in slug for slug in evidence for cue in cues)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def competence_for_question(
    db: Session, question: Question, category: str,
) -> tuple[str, str]:
    """Choose (area, source), never query AI; do not mutate historic metadata.

    A saved reviewed area is authoritative for new questions. Existing
    consultations are frozen in PlatinumHelperCall.topic on first reservation.
    """
    if question.competence_area is not None:
        if question.competence_area not in TOPICS:
            raise ValueError("Stored question has unsupported competence_area")
        return question.competence_area, "question"

    fact = db.get(QuestionFact, question.id)
    concepts = db.scalars(
        select(Tag.slug)
        .join(QuestionTag, QuestionTag.tag_id == Tag.id)
        .where(QuestionTag.question_id == question.id, Tag.kind == "concept")
    ).all()
    inferred = infer_legacy_area(
        fact.subject if fact else None,
        [slug.partition(":")[2] for slug in concepts],
    )
    if inferred is not None:
        return inferred, "legacy_metadata"

    default = CATEGORY_TOPICS.get(category)
    if default is None:
        raise ValueError("No helper competence mapping for question category")
    return default, "legacy_category"
