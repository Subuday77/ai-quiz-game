"""Read-only live Caddy -> Judge evaluation of a quarantined DB question.

No fake game, no QuestionAppeal, no settlement, no wallet writes, no
question status changes. Uses the exact production prompt/schema/model
functions and the stored (possibly WRONG) answer key. This probes Judge
reasoning, not end-to-end compensation.

From backend (PyCharm terminal):
    python -m scripts.judge_quarantined_question b15206ac-7523-4203-9c26-43fe52c4a6fc

Optional: --comment "The 1972 Turing Award was not for COBOL."

Needs configured OPENAI_API_KEY and QUIZ_COMPLAINT_* model environment variables.
Calls both live models; this does incur API usage.
"""
from __future__ import annotations

import argparse
import json
from uuid import UUID

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from persistence.appeal_ai_security import (  # noqa: E402
    judge_messages, keddy_messages, parse_judge, parse_keddy,
)
from persistence.appeal_worker import _completion, _config  # noqa: E402
from persistence.database import engine  # noqa: E402
from persistence.models import Question  # noqa: E402


DEFAULT_COMMENT = (
    "В вопросе, возможно, объединены несовместимые факты: год получения "
    "награды, лауреат и заслуги, за которые она присуждена. "
    "Проверьте каждый из этих фактов и выясните, подходит ли хотя бы один "
    "из предложенных ответов ко всем условиям вопроса."
)


def evaluate(question_id: UUID, *, comment: str = DEFAULT_COMMENT) -> dict:
    """Probe real models with DB question facts; never persist an appeal."""
    with Session(engine()) as db:
        # Protect even against accidental modifications during DB retrieval.
        db.execute(text("SET TRANSACTION READ ONLY"))
        question = db.get(Question, question_id)
        if question is None:
            raise ValueError("Question not found in configured PostgreSQL database")
        if question.status != "quarantined":
            raise ValueError(
                f"Only quarantined questions may be probed; status={question.status!r}"
            )
        if question.generator_model == _config("JUDGE")[0]:
            raise ValueError(
                "Judge model generated this question; independent adjudication required"
            )
        # Copy all required fields while the read-only session is open.
        snapshot = {
            "question_id": str(question.id),
            "status": question.status,
            "question": question.text,
            "answers": {key: question.choices[key] for key in "ABCD"},
            "stored_correct_key": question.correct_key,
            "language": question.language,
            "question_number": question.requested_difficulty + 1,
            "generator_model": question.generator_model,
        }
        db.rollback()

    evidence = {
        "question": snapshot["question"],
        "answers": snapshot["answers"],
        "reasons": ["no_correct"],
        "comment": comment,
        "sources": [],
        "question_number": snapshot["question_number"],
        "language": snapshot["language"],
        "round_context": {
            "before_answer": True, "selected_key": None, "hints_used": 0,
        },
    }
    caddy = parse_keddy(_completion(keddy_messages(evidence), "CADDY"))
    judge = parse_judge(_completion(
        judge_messages(
            evidence,
            correct_key=snapshot["stored_correct_key"],
            preliminary=caddy,
        ),
        "JUDGE",
    ))
    return {
        "mode": "READ_ONLY_MODEL_PROBE",
        "question": snapshot,
        "caddy_model": _config("CADDY")[0],
        "caddy": caddy.model_dump(),
        "judge_model": _config("JUDGE")[0],
        "judge": judge.model_dump(),
        "database_changed": False,
        "stars_charged": False,
        "appeal_created": False,
        "settlement_executed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run real Caddy and Judge on a quarantined DB question, read-only"
    )
    parser.add_argument("question_id", type=UUID, help="Question UUID in PostgreSQL")
    parser.add_argument("--comment", default=DEFAULT_COMMENT,
                        help="Player's hypothetical complaint (max 1000 chars)")
    args = parser.parse_args()
    if not 1 <= len(args.comment) <= 1000:
        parser.error("Complaint must have between 1 and 1000 characters")
    print(json.dumps(
        evaluate(args.question_id, comment=args.comment),
        ensure_ascii=False, indent=2,
    ))
