"""Read all previously displayed user facts; persist verified metadata atomically.

QuestionAttempt.shown_at (not answered_at) is the boundary for repeat history.
All languages and runs belong to the same user-specific history.
"""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from persistence.models import (
    GameRun, Question, QuestionAttempt, QuestionFact, QuestionTag, Tag, Topic,
)
from tagging.contract import TaggedQuestion
from tagging.core import DuplicateQuestionError, HistoryItem, HistoryNotTagged


def history_for_user(db: Session, user_id: UUID) -> list[HistoryItem]:
    rows = db.execute(
        select(Question, QuestionFact, Topic.slug)
        .join(QuestionAttempt, QuestionAttempt.question_id == Question.id)
        .join(GameRun, GameRun.id == QuestionAttempt.run_id)
        .join(Topic, Topic.id == Question.topic_id)
        .outerjoin(QuestionFact, QuestionFact.question_id == Question.id)
        .where(GameRun.user_id == user_id)
        .order_by(QuestionAttempt.shown_at)
    ).all()
    if any(fact is None for _, fact, _ in rows):
        raise HistoryNotTagged("Some previously shown questions need backfill")
    if not rows:
        return []
    unique_ids = list(dict.fromkeys(question.id for question, _, _ in rows))
    tag_map: dict[UUID, dict[str, list[str]]] = {
        question_id: {"entity": [], "concept": [], "keyword": []}
        for question_id in unique_ids
    }
    # Keep SQLite test compatibility and avoid giant SQL IN lists for long histories.
    for offset in range(0, len(unique_ids), 400):
        chunk = unique_ids[offset:offset + 400]
        for question_id, kind, slug in db.execute(
            select(QuestionTag.question_id, Tag.kind, Tag.slug)
            .join(Tag, Tag.id == QuestionTag.tag_id)
            .where(QuestionTag.question_id.in_(chunk))
        ):
            if kind in tag_map[question_id]:
                tag_map[question_id][kind].append(slug.split(":", 1)[-1])
    history = []
    seen = set()
    for question, fact, category_slug in rows:
        if question.id in seen:
            continue
        seen.add(question.id)
        history.append(HistoryItem(
            question_id=str(question.id), category_slug=category_slug,
            question_text=question.text, fact_key=fact.fact_key,
            subject=fact.subject, relation=fact.relation, object=fact.object,
            tags={kind: tuple(slugs) for kind, slugs in tag_map[question.id].items()},
        ))
    return history


def save_metadata(db: Session, question: Question, candidate: TaggedQuestion,
                  *, origin: str = "generator") -> None:
    """Do NOT commit. Caller commits question, tags, fact and attempt together."""
    current = db.get(QuestionFact, question.id)
    if current:
        if current.fact_key != candidate.fact.key:
            raise DuplicateQuestionError("Conflicting canonical fact for existing question")
        return
    db.add(QuestionFact(
        question_id=question.id, fact_key=candidate.fact.key,
        subject=candidate.fact.subject, relation=candidate.fact.relation,
        object=candidate.fact.object, origin=origin,
    ))
    for kind in ("entity", "concept", "keyword"):
        for raw_slug in getattr(candidate.tags, kind):
            slug = f"{kind}:{raw_slug}"
            tag = db.scalar(select(Tag).where(Tag.slug == slug))
            if tag is None:
                # Slug uniqueness is global across users/workers.
                try:
                    with db.begin_nested():
                        tag = Tag(slug=slug, kind=kind,
                                  labels={"en": raw_slug.replace("-", " ")})
                        db.add(tag)
                        db.flush()
                except IntegrityError:
                    tag = db.scalar(select(Tag).where(Tag.slug == slug))
                    if tag is None:
                        raise
            link = db.get(QuestionTag, (question.id, tag.id))
            if link is None:
                db.add(QuestionTag(question_id=question.id, tag_id=tag.id,
                                   origin=origin, weight=100))
