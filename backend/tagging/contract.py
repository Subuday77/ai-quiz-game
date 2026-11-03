"""Canonical, language-independent fact and tag data supplied with AI questions."""
import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from question_schema import Choice, QuizQuestion

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Authorship is one-to-many: one writer may create many different works.
# Historical records sometimes encoded creator -> work (wrote-poem)
# and newer ones work -> creator (author). Both must identify the WORK,
# not collapse every work by an author into a single "fact".
# Extend only for unambiguous relation slugs; unknown relations retain the
# original subject+relation scheme (the answer object stays excluded).
_CREATOR_TO_WORK = {
    "wrote-poem": "written-work",
    "wrote-novel": "written-work",
    "wrote-book": "written-work",
    "wrote-play": "written-work",
    "wrote-work": "written-work",
    "author-of": "written-work",
    "painted-work": "visual-work",
    "painted-painting": "visual-work",
    "composed-work": "musical-work",
}
_WORK_TO_CREATOR = {
    "author": "written-work",
    "written-by": "written-work",
    "poem-author": "written-work",
    "novel-author": "written-work",
    "book-author": "written-work",
    "play-author": "written-work",
    "painter": "visual-work",
    "painted-by": "visual-work",
    "composer": "musical-work",
    "composed-by": "musical-work",
}


def creator_work_anchor(subject: str, relation: str, object: str) -> tuple[str, str] | None:
    """Canonical knowledge identity for a specifically named creative work.

    Return (domain, work slug) regardless of whether the LLM encoded the
    creator or the work as subject. This is a conservative allowlist, not
    an attempt to infer arbitrary natural-language relations.
    """
    if domain := _CREATOR_TO_WORK.get(relation):
        return domain, object
    if domain := _WORK_TO_CREATOR.get(relation):
        return domain, subject
    return None



def canonical(value: str) -> str:
    """Validate stable English ASCII slug; do not silently transliterate facts."""
    if not isinstance(value, str) or len(value) > 140 or not SLUG_PATTERN.fullmatch(value):
        raise ValueError("Canonical tags/facts must use lowercase English kebab-case")
    return value


class TagCloud(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: list[str] = Field(min_length=1, max_length=3)
    concept: list[str] = Field(min_length=1, max_length=4)
    keyword: list[str] = Field(max_length=4)

    @field_validator("entity", "concept", "keyword")
    @classmethod
    def verify_slugs(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Repeated tags")
        return [canonical(value) for value in values]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    relation: str
    object: str

    @field_validator("subject", "relation", "object")
    @classmethod
    def verify_slug(cls, value: str) -> str:
        return canonical(value)

    @property
    def key(self) -> str:
        # For most facts the tested subject+property identify the knowledge;
        # the (possibly wrong) answer object deliberately does not.
        # For known one-to-many creator relations the WORK is the knowledge
        # anchor, even in older creator->work metadata. This prevents the
        # writer of two poems from causing a false duplicate collision.
        anchor = creator_work_anchor(self.subject, self.relation, self.object)
        source = (f"creator-work-v2|{anchor[0]}|{anchor[1]}" if anchor
                  else f"{self.subject}|{self.relation}")
        return hashlib.sha256(source.encode("ascii")).hexdigest()

    @property
    def legacy_key(self) -> str:
        """Pre-v2 hash retained only for comparison with already-stored rows."""
        return hashlib.sha256(
            f"{self.subject}|{self.relation}".encode("ascii")
        ).hexdigest()


class TaggedQuestion(QuizQuestion):
    tags: TagCloud
    fact: Fact
    # Estimated once by the generator, never re-rated by a critic.
    # Optional for approved questions saved under previous schemas.
    plausibility: dict[Choice, int] | None = Field(default=None, exclude_if=lambda value: value is None)
    # Internal server-only audit payload; never part of model_dump/LLM JSON or Angular API.
    _review_report: dict | None = PrivateAttr(default=None)
    _duplicate_assessment: dict | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def fact_in_tags(self) -> "TaggedQuestion":
        if self.fact.subject not in self.tags.entity:
            raise ValueError("Fact subject must appear among entity tags")
        if self.plausibility is not None:
            if set(self.plausibility) != set("ABCD"):
                raise ValueError("Exactly four plausibility scores are required")
            if any(isinstance(score, bool) or not isinstance(score, int)
                   or not 0 <= score <= 100 for score in self.plausibility.values()):
                raise ValueError("Plausibility scores must be integers from 0 to 100")
            # The verified correct answer must never be hidden by a hint.
            self.plausibility = {**self.plausibility, self.correct_answer: 100}
        return self
