"""Strict, deterministic duplicate scoring. AI reviewer can never override it."""
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from generation_policy import InvalidQuestionError
from quality_errors import QualityRegenerationError
from tagging.contract import TaggedQuestion, creator_work_anchor


class DuplicateQuestionError(InvalidQuestionError):
    """Candidate duplicates an already displayed user fact/question."""

    def __init__(self, reason: str, *, score: int | None = None,
                 previous_id: str | None = None):
        super().__init__(reason)
        self.score = score
        self.previous_id = previous_id


class SemanticDuplicateError(QualityRegenerationError):
    """The tag-aware critic found the same previously encountered knowledge."""


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


def _exact_question(value: str) -> str:
    """Normalize only typography, whitespace and terminal question punctuation.

    Keep minus signs, decimal points and other meaningful notation intact:
    '-2' and '2', or '1.5' and '15', must NOT become exact matches.
    """
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.translate(str.maketrans({
        "«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‟": '"',
    }))
    normalized = " ".join(normalized.split())
    normalized = re.sub(r'\s*"\s*', '"', normalized)
    return normalized.rstrip("?!").strip()


def similarity(candidate: TaggedQuestion, previous: HistoryItem) -> Similarity:
    # Verbatim knowledge remains a duplicate regardless of old or new tags.
    if _exact_question(candidate.question) == _exact_question(previous.question_text):
        return Similarity(100, "same_question_text", previous.question_id)
    tags = candidate.tags
    same_entity = bool(set(tags.entity) & set(previous.tags.get("entity", ())))
    same_concept = bool(set(tags.concept) & set(previous.tags.get("concept", ())))

    current_work = creator_work_anchor(
        candidate.fact.subject, candidate.fact.relation, candidate.fact.object,
    )
    previous_work = creator_work_anchor(
        previous.subject, previous.relation, previous.object,
    )
    if current_work is not None and previous_work is not None:
        if current_work == previous_work:
            # Works encoded as author->book and book->author, and historical
            # (pre-v2) fact_key rows, are all compared by the same work.
            return Similarity(100, "same_fact", previous.question_id)
        # Two DIFFERENT named works remain different facts even if the
        # author, subject+relation, genre and question template coincide.
        # In particular, do not give extra 25 relation + 35 wording points
        # for "Бородино" versus "Парус".
        reasons = [name for name, found in (
            ("entity", same_entity), ("concept", same_concept),
        ) if found]
        return Similarity(20 * same_entity + 20 * same_concept,
                          "+".join(reasons) or "different_works", previous.question_id)

    if candidate.fact.key == previous.fact_key:
        return Similarity(100, "same_fact", previous.question_id)

    # Unknown relation slugs retain the original conservative tag+wording
    # scoring; the AI critic can still detect semantic aliases.
    # Sharing a related entity (e.g. the painter Leonardo da Vinci) does NOT
    # make two distinct subjects (Mona Lisa / Last Supper) the same knowledge.
    same_subject = candidate.fact.subject == previous.subject
    same_relation = same_subject and candidate.fact.relation == previous.relation
    same_answer = same_subject and candidate.fact.object == previous.object
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
    if highest and (highest.reason in ("same_fact", "same_question_text") or
                    highest.score >= (threshold() if limit is None else limit)):
        raise DuplicateQuestionError(highest.reason, score=highest.score,
                                     previous_id=highest.previous_id)
    return highest


def generator_context(history: list[HistoryItem], category_slug: str) -> list[dict]:
    """Bounded prompt context; strict check still uses the FULL user history."""
    selected = [item for item in reversed(history) if item.category_slug == category_slug]
    # Include recent nearby facts, not the entire user's history.
    return [{"fact_key": item.fact_key,
             "subject": item.subject, "relation": item.relation,
             "object": item.object,
             "tags": item.tags} for item in selected[:35]]


def critic_neighbors(candidate: TaggedQuestion, history: list[HistoryItem],
                     *, category_slug: str = "") -> list[dict]:
    """Include best tag matches AND recent same-category questions.

    The second group helps the critic recognize aliases across languages
    when different model runs supplied inconsistent English entity slugs.
    """
    lookup = {item.question_id: item for item in history}
    picked: list[HistoryItem] = []
    seen: set[str] = set()
    for score in rank(candidate, history)[:6]:
        if score.score <= 0:
            continue
        item = lookup[score.previous_id]
        if item.question_id not in seen:
            picked.append(item)
            seen.add(item.question_id)
    for item in reversed(history):
        if len(picked) >= 10:
            break
        if item.category_slug == category_slug and item.question_id not in seen:
            picked.append(item)
            seen.add(item.question_id)
    return [{
        "question_id": item.question_id, "question": item.question_text,
        "fact_key": item.fact_key, "subject": item.subject,
        "relation": item.relation, "object": item.object,
    } for item in picked]
