"""Strict, deterministic duplicate scoring. AI reviewer can never override it."""
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from generation_policy import InvalidQuestionError
from tagging.contract import TaggedQuestion


class DuplicateQuestionError(InvalidQuestionError):
    """Candidate duplicates an already displayed user fact/question."""


class HistoryNotTagged(RuntimeError):
    """Some previously shown questions lack verified canonical fact metadata."""


@dataclass(frozen=True)
class HistoryItem:
    question_id: str
    category_slug: str
    question_text: str
    fact_key: str
    subject: str
    relation: str
    object: str
    tags: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Similarity:
    score: int
    reason: str
    previous_id: str


def threshold() -> int:
    value = os.getenv("QUIZ_TAG_DUPLICATE_SCORE_THRESHOLD", "70")
    try:
        number = int(value)
        if not 1 <= number <= 100:
            raise ValueError
        return number
    except ValueError:
        raise ValueError("QUIZ_TAG_DUPLICATE_SCORE_THRESHOLD must be 1..100") from None


def _words(value: str) -> str:
    norm = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", norm, flags=re.UNICODE))


def similarity(candidate: TaggedQuestion, previous: HistoryItem) -> Similarity:
    if candidate.fact.key == previous.fact_key:
        return Similarity(100, "same_fact", previous.question_id)
    tags = candidate.tags
    same_entity = bool(set(tags.entity) & set(previous.tags.get("entity", ())))
    same_concept = bool(set(tags.concept) & set(previous.tags.get("concept", ())))
    # Comparing properties without the same subject is weak evidence:
    # birth-year questions about different people must not be rejected.
    same_relation = same_entity and candidate.fact.relation == previous.relation
    same_answer = same_entity and candidate.fact.object == previous.object
    relative_text = SequenceMatcher(None, _words(candidate.question),
                                    _words(previous.question_text)).ratio()
    wording = relative_text >= .86 and len(_words(candidate.question)) >= 22
    score = (20 * same_entity + 25 * same_relation + 20 * same_concept
             + 20 * same_answer + 35 * wording)
    reasons = [name for name, found in (
        ("entity", same_entity), ("relation", same_relation),
        ("concept", same_concept), ("answer", same_answer),
        ("wording", wording),
    ) if found]
    return Similarity(min(100, score), "+".join(reasons) or "none", previous.question_id)


def rank(candidate: TaggedQuestion, history: list[HistoryItem]) -> list[Similarity]:
    return sorted((similarity(candidate, old) for old in history),
                  key=lambda result: result.score, reverse=True)


def require_unique(candidate: TaggedQuestion, history: list[HistoryItem],
                   *, limit: int | None = None) -> Similarity | None:
    ordered = rank(candidate, history)
    highest = ordered[0] if ordered else None
    if highest and (highest.reason == "same_fact" or
                    highest.score >= (threshold() if limit is None else limit)):
        raise DuplicateQuestionError(highest.reason)
    return highest


def generator_context(history: list[HistoryItem], category_slug: str) -> list[dict]:
    """Bounded prompt context; strict check still uses the FULL user history."""
    selected = [item for item in reversed(history) if item.category_slug == category_slug]
    # Include recent nearby facts, not the entire user's history.
    return [{"fact_key": item.fact_key,
             "subject": item.subject, "relation": item.relation,
             "object": item.object,
             "tags": item.tags} for item in selected[:35]]


def critic_neighbors(candidate: TaggedQuestion, history: list[HistoryItem]) -> list[dict]:
    lookup = {item.question_id: item for item in history}
    result = []
    for s in rank(candidate, history)[:5]:
        if s.score == 0:
            break
        item = lookup[s.previous_id]
        result.append({"question_id": item.question_id, "question": item.question_text,
                       "fact_key": item.fact_key, "subject": item.subject,
                       "relation": item.relation, "object": item.object,
                       "score": s.score})
    return result
