"""Quarantine the verified false 1972 Turing Award question, without deleting history.

Run from backend with the same DATABASE_URL as the FastAPI service:
    python -m persistence.quarantine_known_bad
    python -m persistence.quarantine_known_bad --apply

Default is dry-run. Stop FastAPI before --apply to avoid active-session races.
Records and historic answers are NOT deleted or rewritten. The original critic
verdict is retained, with a separate explicit audit entry on application.
"""
import argparse

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.database import engine
from persistence.models import Question, QuestionFact, QuestionReview
from tagging.quality_skill.fact_tool import known_false_triple

TARGET_STEM = (
    "Какой учёный получил Премию Тьюринга 1972 года за вклад "
    "в создание и развитие языка программирования COBOL?"
)
TARGET_CORRECT_KEY = "B"
TARGET_ANSWER = "Грейс Хоппер"

AUDIT_SOURCE = "https://amturing.acm.org/laureates.cfm"
AUDIT_CODE = "acm_turing_1972_false_recipient"


def quarantine_known_bad(db: Session, *, apply: bool = False) -> dict:
    """Exact-match, transactional, idempotent and fail-closed DB correction."""
    try:
        questions = db.scalars(
            select(Question).where(Question.text == TARGET_STEM).with_for_update()
        ).all()
        if not questions:
            db.rollback()
            return {"matched": 0, "changed": 0, "already_quarantined": 0,
                    "applied": apply}
        # Validate the WHOLE batch before updating any item. A question with
        # different metadata is never quarantined by this maintenance action.
        for q in questions:
            fact = db.get(QuestionFact, q.id)
            if (q.correct_key != TARGET_CORRECT_KEY
                    or q.choices.get(TARGET_CORRECT_KEY) != TARGET_ANSWER
                    or fact is None or known_false_triple(
                        fact.subject, fact.relation, fact.object
                    ) is None):
                # Fact has the same subject/relation/object names as
                # TaggedQuestion.fact, so it can be checked by a wrapper.
                raise ValueError(
                    "Stored question differs from verified incident; no update"
                )
            if q.status not in {"approved", "quarantined"}:
                raise ValueError("Unexpected question state; no update")
        already = sum(q.status == "quarantined" for q in questions)
        changed = len(questions) - already
        if apply:
            for q in questions:
                if q.status == "quarantined":
                    continue
                q.status = "quarantined"
                db.add(QuestionReview(
                    question_id=q.id,
                    checker_type="verified_fact_correction",
                    provider=None,
                    model=None,
                    contract_version="manual_fact_audit_v1",
                    decision="quarantine",
                    issues=["factual_error", "incorrect_key"],
                    details={
                        "code": AUDIT_CODE,
                        "source": AUDIT_SOURCE,
                        "reason": "1972 ACM Turing Award was awarded to Edsger W. Dijkstra",
                    },
                    duration_ms=0,
                ))
            db.commit()
        else:
            db.rollback()
        return {"matched": len(questions), "changed": changed,
                "already_quarantined": already, "applied": apply}
    except Exception:
        db.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Commit exact-match quarantine; otherwise dry-run")
    args = parser.parse_args()
    load_dotenv(find_dotenv())
    with Session(engine(), autoflush=False) as db:
        result = quarantine_known_bad(db, apply=args.apply)
    print("verified_question_quarantine:", result)
    if result["matched"] == 0:
        print("Question was not found in this database; check DATABASE_URL.")


if __name__ == "__main__":
    main()
