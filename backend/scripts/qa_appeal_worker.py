"""Isolated, real appeal-worker / wallet E2E *backend* QA.

Creates a throwaway account, game and a COPY of a known quarantined question.
Uses production file_appeal() and process_appeal() (real Keddy and Judge).
The source question, real users and their wallets are never modified.
The copied question is quarantined by file_appeal() BEFORE the fixture commits.

Stop FastAPI first to prevent the background appeal scheduler racing this test.
From the backend folder (PyCharm terminal), with PostgreSQL migrated:

    $env:QUIZ_QA_APPEAL_WORKER_TEST_ENABLED="true"
    python -m scripts.qa_appeal_worker prepare --scenario q10 --confirm QA-WORKER-TEST
    python -m scripts.qa_appeal_worker run --appeal-id <APPEAL_UUID> --confirm QA-WORKER-TEST
    python -m scripts.qa_appeal_worker verify --appeal-id <APPEAL_UUID>

Repeat with --scenario q16. Both prepare and run change the database and
require the explicit environment opt-in and confirmation. Verify is read-only.

The live run invokes two OpenAI models and incurs API costs. It NEVER invokes
process_due(): only the exact prepared QA complaint is processed. No cleanup
is automatic; the ledger is retained for audit and the isolated copy remains
quarantined or rejected, never available to ordinary players.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from persistence.appeal_service import file_appeal, quote_appeal  # noqa: E402
from persistence.appeal_worker import process_appeal  # noqa: E402
from persistence.database import engine  # noqa: E402
from persistence.models import (  # noqa: E402
    GameRun, Question, QuestionAppeal, QuestionAttempt, QuestionModeration,
    Topic, User, WalletTransaction,
)
from persistence.star_economy import star_balances  # noqa: E402

SOURCE_QUESTION_ID = UUID("b15206ac-7523-4203-9c26-43fe52c4a6fc")
ENABLE_FLAG = "QUIZ_QA_APPEAL_WORKER_TEST_ENABLED"
CONFIRM_TEXT = "QA-WORKER-TEST"
USERNAME_PREFIX = "QAWorker"
TOPIC_PREFIX = "qa-appeal-worker-"
GENERATOR_PROVIDER = "qa_worker_fixture"
PROMPT_VERSION = "qa_appeal_worker_v1"
COMMENT = (
    "В формулировке объединены неверные сведения: лауреат премии Тьюринга "
    "1972 года и вклад в COBOL. Проверьте факты независимо: ни один вариант "
    "не соответствует одновременно году награды и указанной заслуге."
)
TIERS = ("bronze", "silver", "gold", "platinum")


def require_write_opt_in(confirm: str) -> None:
    if (os.getenv(ENABLE_FLAG, "").strip().lower() != "true"
            or confirm != CONFIRM_TEXT):
        raise ValueError(
            f"Write operation requires {ENABLE_FLAG}=true and "
            f"--confirm {CONFIRM_TEXT}"
        )


def _qa_case(db: Session, appeal_id: UUID):
    """Do not let this CLI operate on a normal player's appeal by UUID."""
    appeal = db.get(QuestionAppeal, appeal_id)
    if appeal is None or appeal.user_id is None or appeal.run_id is None:
        raise ValueError("QA appeal not found")
    user = db.get(User, appeal.user_id)
    run = db.get(GameRun, appeal.run_id)
    question = db.get(Question, appeal.question_id)
    topic = db.get(Topic, question.topic_id) if question else None
    if (user is None or run is None or question is None or topic is None
            or not user.username.startswith(USERNAME_PREFIX)
            or not user.username_key.startswith(USERNAME_PREFIX.lower())
            or not topic.slug.startswith(TOPIC_PREFIX)
            or question.generator_provider != GENERATOR_PROVIDER
            or question.prompt_version != PROMPT_VERSION
            or run.user_id != user.id
            or appeal.question_number not in (10, 16)
            or appeal.before_answer
            or appeal.context.get("run_status_at_submission") != "lost"
            or appeal.context.get("lost_question_id") != str(question.id)):
        raise ValueError("Refusing non-QA appeal or malformed QA fixture")
    return appeal, user, run, question


def prepare(*, scenario: str, source_question_id: UUID = SOURCE_QUESTION_ID,
            confirm: str = "") -> dict:
    require_write_opt_in(confirm)
    if scenario not in ("q10", "q16"):
        raise ValueError("Unknown QA scenario; choose q10 or q16")
    number = 10 if scenario == "q10" else 16
    now = datetime.now(timezone.utc)
    suffix = uuid4().hex[:12]
    with Session(engine(), expire_on_commit=False) as db:
        source = db.get(Question, source_question_id)
        if source is None or source.status != "quarantined":
            raise ValueError("Source question must exist and be quarantined")
        if (source.correct_key != "B" or
                not all(key in source.choices for key in "ABCD")):
            raise ValueError("The known invalid source question has changed")
        judge_model = os.getenv("QUIZ_COMPLAINT_JUDGE_MODEL_ID", "").strip()
        if not judge_model or source.generator_model == judge_model:
            raise ValueError("Independent Judge model must be configured")

        user = User(
            username=USERNAME_PREFIX + suffix,
            username_key=(USERNAME_PREFIX + suffix).casefold(),
            display_name="Isolated worker QA " + scenario,
        )
        topic = Topic(
            slug=TOPIC_PREFIX + suffix,
            labels={"en": "Isolated worker QA", "ru": "Тест воркера"},
        )
        db.add_all([user, topic])
        db.flush()
        run = GameRun(
            user_id=user.id,
            language=source.language,
            status="lost",
            correct_main_answers=number - 1 if number == 10 else 15,
            bonus_wager_accepted=number == 16,
            bonus_correct=False if number == 16 else None,
            finished_at=now,
        )
        db.add(run)
        db.flush()
        question = Question(
            topic_id=topic.id, language=source.language,
            requested_difficulty=number - 1, text=source.text,
            choices={key: source.choices[key] for key in "ABCD"},
            correct_key=source.correct_key,
            fingerprint=uuid4().hex + uuid4().hex,
            status="approved", generator_provider=GENERATOR_PROVIDER,
            generator_model=source.generator_model,
            prompt_version=PROMPT_VERSION, schema_version=source.schema_version,
        )
        db.add(question)
        db.flush()
        attempt = QuestionAttempt(
            run_id=run.id, question_id=question.id,
            number=number, requested_difficulty=number - 1,
            selected_key="A", is_correct=False,
            answered_at=now, hints_used=1 if number == 10 else 0,
            appeal_expenses={"hint_bronze": 2} if number == 10 else {},
        )
        db.add(attempt)
        db.flush()

        # Standalone user has no authentication identity, so cannot be used
        # for interactive play; all transactions are namespaced to this run.
        def wallet(tier: str, amount: int, action: str) -> None:
            db.add(WalletTransaction(
                user_id=user.id, run_id=run.id,
                idempotency_key=f"qa-worker:{suffix}:{action}",
                amount=amount, reason=f"star/{tier}/qa-worker-{action}",
            ))

        if number == 10:
            wallet("bronze", 6, "seed-bronze")
            wallet("silver", 4, "seed-silver")
            wallet("bronze", -2, "hint-1")
            before = {"bronze": 4, "silver": 4, "gold": 0, "platinum": 0}
        else:
            wallet("gold", 4, "seed-gold")
            wallet("gold", -1, "bonus-wager")
            before = {"bronze": 0, "silver": 0, "gold": 3, "platinum": 0}
        db.flush()
        if star_balances(db, user.id) != before:
            raise AssertionError("QA seed balance mismatch")
        quote = quote_appeal(
            db, run_id=run.id, user_id=user.id, question_id=question.id,
        )
        if (quote["deposit_tier"] != ("silver" if number == 10 else "gold")
                or quote["filing_fee_bronze"] != 0 or quote["before_answer"]):
            raise AssertionError("Unexpected QA filing quote")
        appeal = file_appeal(
            db, user_id=user.id, run_id=run.id,
            question_id=question.id, request_id=uuid4(),
            reasons=["no_correct"], comment=COMMENT, source_links=[],
            confirm_nonrefundable_fee=False, expected_fee_bronze=0,
        )
        # file_appeal() commits the fixture + the actual deposit atomically.
        if db.get(Question, question.id).status != "quarantined":
            raise AssertionError("QA copy must be quarantined on filing")
        after = star_balances(db, user.id)
        expected_pending = (
            {"bronze": 4, "silver": 3, "gold": 0, "platinum": 0}
            if number == 10 else
            {"bronze": 0, "silver": 0, "gold": 2, "platinum": 0}
        )
        if after != expected_pending:
            raise AssertionError("QA deposit or seed debit mismatch")
        return {
            "mode": "PREPARED", "scenario": scenario,
            "source_question_id": str(source_question_id),
            "copy_question_id": str(question.id),
            "username": user.username, "user_id": str(user.id),
            "run_id": str(run.id), "appeal_id": str(appeal.id),
            "appeal_status": appeal.status, "question_status": "quarantined",
            "before_filing": before, "after_filing": after,
            "fee_bronze": appeal.filing_fee_bronze,
            "deposit_tier": appeal.deposit_tier,
            "next_command": (
                "python -m scripts.qa_appeal_worker run --appeal-id "
                + str(appeal.id) + " --confirm " + CONFIRM_TEXT
            ),
        }


def verify(*, appeal_id: UUID) -> dict:
    """Read-only report: compare exact ledger actions, balances and moderation."""
    with Session(engine()) as db:
        appeal, user, run, question = _qa_case(db, appeal_id)
        number = appeal.question_number
        actions = list(db.scalars(select(WalletTransaction).where(
            WalletTransaction.idempotency_key.like(f"appeal:{appeal_id}:%"),
        )))
        entries = {
            item.idempotency_key.split(":")[-1]: {
                "tier": item.reason.split("/")[1],
                "amount": item.amount,
            }
            for item in actions
        }
        expected = {
            "deposit": {"tier": "silver" if number == 10 else "gold", "amount": -1},
            "appeal-deposit-refund": {
                "tier": "silver" if number == 10 else "gold", "amount": 1,
            },
            "appeal-compensation": {
                "tier": "silver" if number == 10 else "platinum", "amount": 1,
            },
        }
        if number == 10:
            expected["appeal-hint-refund"] = {"tier": "bronze", "amount": 2}
        else:
            expected["appeal-bonus-wager-refund"] = {"tier": "gold", "amount": 1}
        balances = star_balances(db, user.id)
        expected_balances = (
            {"bronze": 6, "silver": 5, "gold": 0, "platinum": 0}
            if number == 10 else
            {"bronze": 0, "silver": 0, "gold": 4, "platinum": 1}
        )
        moderation = db.get(QuestionModeration, question.id)
        checks = {
            "upheld": appeal.status == "upheld",
            "exact_ledger_once": len(actions) == len(expected) and entries == expected,
            "balances": balances == expected_balances,
            "deposit_refunded": appeal.deposit_refunded_at is not None,
            "compensation_credited": appeal.compensated_at is not None,
            "wager_refunded_if_q16": (
                bool(appeal.bonus_wager_refunded_at) == (number == 16)
            ),
            "question_blocked": question.status == "rejected",
            "moderation": bool(moderation is not None
                               and moderation.distinct_complainants == 1
                               and moderation.upheld_count == 1
                               and moderation.permanently_blocked
                               and moderation.block_reason == "confirmed_error"),
            "lost_run_preserved": run.status == "lost",
            "no_nonrefundable_fee": appeal.filing_fee_bronze == 0,
            "snapshot_unchanged": (appeal.question_snapshot["question"] == question.text
                                   and appeal.context["correct_key"] == "B"),
        }
        return {
            "mode": "VERIFY", "scenario": "q10" if number == 10 else "q16",
            "appeal_id": str(appeal.id), "user_id": str(user.id),
            "question_id": str(question.id), "status": appeal.status,
            "judge_attempts": appeal.judge_attempts,
            "verdict": appeal.verdict_localized,
            "checks": checks, "passed": all(checks.values()),
            "actual_ledger": entries, "expected_ledger": expected,
            "actual_balances": balances, "expected_balances": expected_balances,
        }


def run_worker(*, appeal_id: UUID, confirm: str = "") -> dict:
    require_write_opt_in(confirm)
    # Only a fixture tagged at creation by our test script may be evaluated.
    with Session(engine()) as db:
        appeal, _, _, _ = _qa_case(db, appeal_id)
        prior_status = appeal.status
        db.rollback()
    # No process_due(): never touch actual pending players' complaints.
    result = process_appeal(appeal_id)
    report = verify(appeal_id=appeal_id)
    report.update(mode="WORKER_RUN", worker_result=result,
                  previous_status=prior_status)
    if report["status"] in ("upheld", "rejected", "technical_unresolved"):
        # Reprocessing must not make extra credits or call providers again.
        repeated = process_appeal(appeal_id)
        after = verify(appeal_id=appeal_id)
        report["idempotency"] = {
            "second_call_result": repeated,
            "no_extra_credits": (
                after["actual_ledger"] == report["actual_ledger"]
                and after["actual_balances"] == report["actual_balances"]
            ),
        }
        report["passed"] = report["passed"] and report["idempotency"]["no_extra_credits"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated real appeal worker and ledger QA")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = sub.add_parser("prepare", help="Create and file a QA-only appeal")
    prepare_cmd.add_argument("--scenario", choices=("q10", "q16"), required=True)
    prepare_cmd.add_argument("--source-question-id", type=UUID, default=SOURCE_QUESTION_ID)
    prepare_cmd.add_argument("--confirm", default="")
    run_cmd = sub.add_parser("run", help="Invoke the real worker for this QA appeal UUID")
    run_cmd.add_argument("--appeal-id", type=UUID, required=True)
    run_cmd.add_argument("--confirm", default="")
    verify_cmd = sub.add_parser("verify", help="Read-only ledger and payout report")
    verify_cmd.add_argument("--appeal-id", type=UUID, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(scenario=args.scenario, confirm=args.confirm,
                         source_question_id=args.source_question_id)
    elif args.command == "run":
        result = run_worker(appeal_id=args.appeal_id, confirm=args.confirm)
    else:
        result = verify(appeal_id=args.appeal_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Failed verification and retry-pending MUST not be reported as a pass.
    if args.command != "prepare" and not result["passed"]:
        parser.exit(1, "QA worker checks did not pass; fixture retained for inspection.\n")


if __name__ == "__main__":
    main()
