"""Platinum Helper domain rules. No provider calls, HTTP, or database access.

A purchased helper profile is generated once and must be persisted by its caller.
Consultation attempts are identified by a stable idempotency key so HTTP retries
cannot reroll the outcome or spend multiple calls.
"""
import hashlib
from dataclasses import dataclass, field
from random import Random
from typing import Literal
from uuid import UUID, uuid4

Skill = Literal["HIGH", "MEDIUM", "LOW"]
Outcome = Literal["KNOW", "MISTAKEN", "UNSURE"]

TOPICS = (
    "Physics", "Mathematics", "Chemistry", "Biology", "Medicine",
    "History", "Geography", "Literature", "Art", "Music", "Cinema",
    "Mythology", "Sports", "Technology", "Astronomy", "Meteorology",
    "Culture", "Engineering", "Agriculture", "Comics",
)
PROFESSIONAL_SKILLS: dict[str, tuple[str, ...]] = {
    "Firefighter": ("Engineering", "Chemistry"),
    "Physicist": ("Physics", "Mathematics"),
    "Mathematician": ("Mathematics",),
    "Plumber": ("Engineering",),
    "Doctor": ("Medicine", "Biology"),
    "Historian": ("History",),
    "Chef": ("Agriculture", "Chemistry"),
    "Musician": ("Music",),
    "Astronomer": ("Astronomy", "Physics"),
    "Engineer": ("Engineering", "Technology"),
    "Librarian": ("Literature",),
    "Athlete": ("Sports",),
}

# A related area may improve to HIGH through erudition but never roll LOW.
# Keep these guarantees within the same twenty canonical knowledge areas.
PROFESSIONAL_RELATED_SKILLS: dict[str, tuple[str, ...]] = {
    "Firefighter": ("Physics", "Medicine", "Meteorology"),
    "Physicist": ("Astronomy", "Engineering", "Chemistry"),
    "Mathematician": ("Physics", "Technology", "Astronomy"),
    "Plumber": ("Physics", "Chemistry", "Technology"),
    "Doctor": ("Chemistry", "Physics"),
    "Historian": ("Mythology", "Culture", "Geography", "Literature"),
    "Chef": ("Biology", "Culture", "Medicine"),
    "Musician": ("Art", "Culture", "Mathematics"),
    "Astronomer": ("Mathematics", "Geography", "Technology"),
    "Engineer": ("Physics", "Mathematics", "Chemistry"),
    "Librarian": ("History", "Culture", "Mythology"),
    "Athlete": ("Biology", "Medicine"),
}
assert set(PROFESSIONAL_RELATED_SKILLS) == set(PROFESSIONAL_SKILLS)
assert all(
    set(related) <= set(TOPICS) and not (set(related) & set(PROFESSIONAL_SKILLS[profession]))
    for profession, related in PROFESSIONAL_RELATED_SKILLS.items()
)

# The actual game uses about 50 fine-grained categories. Each maps to exactly
# one of the twenty broad profile areas; never trust a client-supplied topic.
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
KNOW_DECAY_PER_QUESTION = {"HIGH": 0.5, "MEDIUM": 2.5, "LOW": 5.0}


def outcome_weights(skill: Skill, question_number: int) -> tuple[float, float, float]:
    """KNOW, MISTAKEN, UNSURE percentages for main-round questions 1..15."""
    if not 1 <= question_number <= 15:
        raise ValueError("Platinum Helper is only available for questions 1..15")
    know = 100.0 - KNOW_DECAY_PER_QUESTION[skill] * question_number
    missing = 100.0 - know
    mistaken = missing / 3.0
    unsure = missing - mistaken
    return know, mistaken, unsure
MAX_CONSULTATIONS = 3

PresentationStyle = Literal["DIRECT", "PERSONAL", "CASUAL"]


def choose_presentation_style(call_id: UUID) -> PresentationStyle:
    """Stable, correctness-independent verbal frame; no extra DB column.

    Uses a different salted hash from delivery_style, based solely on the
    random persisted consultation UUID. Retries cannot reroll either style.
    """
    digest = hashlib.sha256(b"helper-speech-presentation-v1:" + call_id.bytes).digest()
    return ("DIRECT", "PERSONAL", "CASUAL")[digest[0] % 3]


DeliveryStyle = Literal["CONFIDENT", "TENTATIVE"]


def choose_delivery_style(call_id: UUID) -> DeliveryStyle:
    """Stable 50/50 speech style, independent of correctness and competence.

    Each persisted consultation has a random UUID. Deriving style from it
    freezes the style across failures and retries without a schema migration.
    """
    return "CONFIDENT" if call_id.int & 1 == 0 else "TENTATIVE"



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
    gender: str = ""
    nationality: str = ""
    tagline_id: int = 1
    tagline_high: str = ""
    tagline_low: str = ""

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
    tagline: str = "",
    rng: Random | None = None,
    gender: str = "",
    nationality: str = "",
) -> HelperProfile:
    rng = rng or Random()
    if profession not in PROFESSIONAL_SKILLS:
        raise ValueError("Unsupported helper profession")
    erudition = round(max(0, min(100, rng.gauss(50, 15))))
    locked = set(PROFESSIONAL_SKILLS[profession])
    # Step 1: a Gaussian erudition score defines the number of extra HIGH areas.
    # Step 2: pick their identities independently of profession.
    high_bands = (
        (20, 0, 1),
        (40, 2, 3),
        (60, 4, 6),
        (80, 7, 9),
        (100, 10, 12),
    )
    extra_high_count = next(
        rng.randint(low, high)
        for limit, low, high in high_bands
        if erudition <= limit
    )
    candidates = [topic for topic in TOPICS if topic not in locked]
    high_topics_set = locked | set(rng.sample(
        candidates, min(extra_high_count, len(candidates))
    ))
    related = set(PROFESSIONAL_RELATED_SKILLS[profession])
    # As erudition increases, the remaining non-HIGH skills shift LOW -> MEDIUM.
    medium_probability = 0.15 + 0.70 * (erudition / 100)
    skills: dict[str, Skill] = {
        topic: ("HIGH" if topic in high_topics_set else
                "MEDIUM" if topic in related or rng.random() < medium_probability else "LOW")
        for topic in TOPICS
    }
    high_topics = [topic for topic, level in skills.items() if level == "HIGH"]
    low_topics = [topic for topic, level in skills.items() if level == "LOW"]
    featured_high = tuple(rng.sample(high_topics, min(len(high_topics), rng.choice((2, 3)))))
    featured_low = tuple(rng.sample(low_topics, min(len(low_topics), rng.choice((2, 3)))))
    return HelperProfile(
        id=str(uuid4()), name=name, profession=profession, erudition=erudition,
        skills=skills, featured_high=featured_high, featured_low=featured_low,
        tagline=tagline, gender=gender, nationality=nationality,
        tagline_id=rng.randint(1, 4),
        tagline_high=rng.choice(featured_high) if featured_high else profession,
        tagline_low=rng.choice(featured_low) if featured_low else "",

    )


def choose_outcome(skill: Skill, rng: Random, question_number: int = 1) -> Outcome:
    weights = outcome_weights(skill, question_number)
    draw = rng.random() * 100.0
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
              options: tuple[str, ...], correct: str, *, rng: Random | None = None,
              question_number: int = 1) -> Consultation:
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
        outcome = choose_outcome(self.profile.skills[topic], rng, question_number)
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
