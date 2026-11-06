"""Platinum Helper domain rules. No provider calls, HTTP, or database access.

A purchased helper profile is generated once and must be persisted by its caller.
Consultation attempts are identified by a stable idempotency key so HTTP retries
cannot reroll the outcome or spend multiple calls.
"""
from dataclasses import dataclass, field
from random import Random
from typing import Literal
from uuid import uuid4

Skill = Literal["HIGH", "MEDIUM", "LOW"]
Outcome = Literal["KNOW", "MISTAKEN", "UNSURE"]

TOPICS = (
    "Physics", "Mathematics", "Chemistry", "Biology", "Medicine",
    "History", "Geography", "Literature", "Art", "Music", "Cinema",
    "Mythology", "Sports", "Technology", "Astronomy", "Meteorology",
    "Fire Safety", "Engineering", "Agriculture", "Comics",
)
PROFESSIONAL_SKILLS: dict[str, tuple[str, ...]] = {
    "Firefighter": ("Fire Safety", "Technology"),
}
OUTCOME_WEIGHTS = {
    "HIGH": (90, 8, 2),
    "MEDIUM": (45, 30, 25),
    "LOW": (5, 15, 80),
}
MAX_CONSULTATIONS = 3


@dataclass(frozen=True)
class HelperProfile:
    id: str
    name: str
    profession: str
    erudition: int
    skills: dict[str, Skill]
    featured_high: tuple[str, ...]
    featured_low: tuple[str, ...]
    tagline: str

    def public_card(self, remaining: int) -> dict:
        """Never expose the private complete skill map to the client."""
        return {
            "id": self.id,
            "name": self.name,
            "profession": self.profession,
            "erudition": self.erudition,
            "strong_topics": list(self.featured_high),
            "weak_topics": list(self.featured_low),
            "tagline": self.tagline,
            "consultations_remaining": remaining,
            "consultations_total": MAX_CONSULTATIONS,
        }


def generate_profile(
    profession: str = "Firefighter",
    *,
    name: str = "Alex",
    tagline: str = "I know a thing or two about fires. Ancient gods? Not so much.",
    rng: Random | None = None,
) -> HelperProfile:
    rng = rng or Random()
    if profession not in PROFESSIONAL_SKILLS:
        raise ValueError("Unsupported helper profession")
    erudition = round(max(0, min(100, rng.gauss(50, 15))))
    locked = set(PROFESSIONAL_SKILLS[profession])
    skills: dict[str, Skill] = {}
    for topic in TOPICS:
        if topic in locked:
            skills[topic] = "HIGH"
            continue
        # Smoothly shift LOW to HIGH as erudition rises; MEDIUM peaks near 50.
        high = 0.04 + 0.62 * (erudition / 100) ** 1.5
        medium = 0.18 + 0.23 * (1 - abs(erudition - 50) / 50)
        draw = rng.random()
        skills[topic] = "HIGH" if draw < high else "MEDIUM" if draw < high + medium else "LOW"
    high_topics = [topic for topic, level in skills.items() if level == "HIGH"]
    low_topics = [topic for topic, level in skills.items() if level == "LOW"]
    featured_high = tuple(rng.sample(high_topics, min(len(high_topics), rng.choice((2, 3)))))
    featured_low = tuple(rng.sample(low_topics, min(len(low_topics), rng.choice((2, 3)))))
    return HelperProfile(
        id=str(uuid4()), name=name, profession=profession, erudition=erudition,
        skills=skills, featured_high=featured_high, featured_low=featured_low,
        tagline=tagline,
    )


def choose_outcome(skill: Skill, rng: Random) -> Outcome:
    weights = OUTCOME_WEIGHTS[skill]
    draw = rng.randrange(sum(weights))
    if draw < weights[0]:
        return "KNOW"
    if draw < weights[0] + weights[1]:
        return "MISTAKEN"
    return "UNSURE"


@dataclass
class Consultation:
    request_id: str
    question_id: str
    topic: str
    outcome: Outcome
    suggested_answer: str | None
    response: str | None = None
    charged: bool = False


@dataclass
class HelperService:
    """Pure state holder; persist and lock per user/purchase in the API layer."""

    profile: HelperProfile
    remaining: int = MAX_CONSULTATIONS
    consultations: dict[str, Consultation] = field(default_factory=dict)

    def start(self, request_id: str, question_id: str, topic: str,
              options: tuple[str, ...], correct: str, *, rng: Random | None = None) -> Consultation:
        if not request_id or not question_id:
            raise ValueError("Missing idempotency or question ID")
        previous = self.consultations.get(request_id)
        if previous:
            if previous.question_id != question_id or previous.topic != topic:
                raise ValueError("Idempotency key reused for another question")
            return previous
        if self.remaining <= 0:
            raise ValueError("No consultations remaining")
        if topic not in self.profile.skills:
            raise ValueError("Unsupported topic")
        if len(options) != 4 or len(set(options)) != 4 or correct not in options:
            raise ValueError("Invalid question options")
        rng = rng or Random()
        outcome = choose_outcome(self.profile.skills[topic], rng)
        wrong = rng.choice([option for option in options if option != correct]) if outcome == "MISTAKEN" else None
        consultation = Consultation(request_id, question_id, topic, outcome, wrong)
        self.consultations[request_id] = consultation
        return consultation

    def complete(self, request_id: str, response: str, *, verified: bool) -> Consultation:
        consultation = self.consultations[request_id]
        if consultation.charged:
            return consultation
        if not verified or not response.strip():
            # A technical error does not consume a call; retry the same request ID.
            return consultation
        if self.remaining <= 0:
            raise ValueError("No consultations remaining")
        self.remaining -= 1
        consultation.response = response
        consultation.charged = True
        return consultation
