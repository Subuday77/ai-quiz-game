"""Canonical, language-independent fact and tag data supplied with AI questions."""
import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from question_schema import QuizQuestion

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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
        # Subject + specific relation identify the KNOWLEDGE BEING TESTED.
        # Answer is intentionally excluded: the same question with changed
        # answer options or another claimed answer remains a duplicate.
        return hashlib.sha256(f"{self.subject}|{self.relation}".encode("ascii")).hexdigest()


class TaggedQuestion(QuizQuestion):
    tags: TagCloud
    fact: Fact

    @model_validator(mode="after")
    def fact_in_tags(self) -> "TaggedQuestion":
        if self.fact.subject not in self.tags.entity:
            raise ValueError("Fact subject must appear among entity tags")
        return self
