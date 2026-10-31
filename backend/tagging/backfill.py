"""Explicit, resumable historical fact/tag extraction (never on server startup).

From backend/ after migration:
    python -m tagging.backfill --dry-run
    python -m tagging.backfill --apply --limit 20

Without --apply, no provider call, write, or charge occurs.
"""
import argparse
import json
from pathlib import Path
from time import sleep

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import MAX_OUTPUT_TOKENS
from define_ai_provider import LLMClient
from output_contracts import OutputContract
from persistence.database import engine
from persistence.models import Question, QuestionFact
from provider_result import completed_text
from quality_configuration import ReviewerSettings
from question_generator import normalize_output
from tagging.contract import Fact, TagCloud, TaggedQuestion
from tagging.repository import save_metadata

EXTRACTION_PROMPT = """
Tag this EXISTING multiple-choice question. Do not invent or modify the
question, answers or correct key. Return a strict JSON with:
tags: {entity: 1-3 English kebab-case slugs,
       concept: 1-4 English kebab-case slugs,
       keyword: 0-4 English kebab-case slugs}
fact: {subject: English kebab-case slug, relation: English kebab-case slug,
       object: English kebab-case slug}
Use stable canonical English entity IDs across languages, e.g.
'albert-einstein'. The subject MUST be included among tags.entity.
The relation must be specifically what is being tested, e.g. birth-year,
not broad words like history, identity or name.
The object expresses the known correct answer. Do not tag the difficulty.
Even if another language paraphrases this exact question, produce the
same subject and relation. The output is metadata ONLY, not a new question.
"""


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: TagCloud
    fact: Fact


def _extract(question: Question) -> TaggedQuestion:
    settings = ReviewerSettings.from_env()
    client = LLMClient(
        settings.provider, model_id=settings.model_id,
        structured_output_mode=settings.structured_output_mode, role="reviewer",
    )
    raw = client.responses.create(
        instructions=EXTRACTION_PROMPT,
        input=json.dumps({
            "language": question.language,
            "question": question.text,
            "answers": question.choices,
            "correct_answer": question.correct_key,
            "correct_answer_text": question.choices[question.correct_key],
        }, ensure_ascii=False),
        max_output_tokens=max(MAX_OUTPUT_TOKENS, 800),
        reasoning={"effort": "low"},
        output_contract=OutputContract.TAG_EXTRACTION,
    )
    metadata = Extraction.model_validate(json.loads(normalize_output(completed_text(raw))))
    return TaggedQuestion.model_validate({
        "difficulty": question.requested_difficulty,
        "question": question.text, "answers": question.choices,
        "correct_answer": question.correct_key,
        "tags": metadata.tags.model_dump(), "fact": metadata.fact.model_dump(),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill existing question facts and tags.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Count missing facts; no AI calls (default).")
    mode.add_argument("--apply", action="store_true", help="Call critic model and write verified metadata.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum historical questions to process (required with --apply).")
    parser.add_argument("--retries", type=int, default=2, help="Validation retries per question, default 2.")
    parser.add_argument("--pause", type=float, default=0, help="Delay between AI requests in seconds.")
    args = parser.parse_args()
    if args.apply and (args.limit is None or args.limit < 1):
        parser.error("--apply requires a positive --limit to bound paid calls")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not 1 <= args.retries <= 3:
        parser.error("--retries must be between 1 and 3")
    if not 0 <= args.pause <= 60:
        parser.error("--pause must be between 0 and 60")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    with Session(engine(), autoflush=False, expire_on_commit=False) as db:
        missing = (
            select(Question)
            .outerjoin(QuestionFact, QuestionFact.question_id == Question.id)
            .where(QuestionFact.question_id.is_(None))
            .order_by(Question.created_at, Question.id)
        )
        remaining = db.scalar(select(func.count()).select_from(missing.subquery())) or 0
        print(f"Historical questions missing canonical tags/facts: {remaining}")
        if not args.apply:
            print("DRY RUN: no AI calls, database unchanged.")
            return

        queue = db.scalars(missing.limit(args.limit)).all()
        db.rollback()  # Keep connections free between calls.
        completed, failures = 0, 0
        for question in queue:
            tagged = None
            for attempt in range(1, args.retries + 1):
                try:
                    tagged = _extract(question)
                    break
                except Exception as exc:
                    # Never log user content or credentials to the console.
                    print(f"Question {question.id}: extraction {attempt} failed ({type(exc).__name__})")
            if tagged is None:
                failures += 1
                continue
            try:
                # Resumable: another worker might have tagged this question already.
                if db.get(QuestionFact, question.id) is None:
                    save_metadata(db, question, tagged, origin="backfill")
                    db.commit()
                completed += 1
            except Exception as exc:
                db.rollback()
                failures += 1
                print(f"Question {question.id}: database save failed ({type(exc).__name__})")
            if args.pause:
                sleep(args.pause)
        print(f"Backfill completed: {completed}, failures: {failures}.")
        print("Re-run --dry-run and review samples before AI_TAG_DEDUP_ENABLED=true.")


if __name__ == "__main__":
    main()
