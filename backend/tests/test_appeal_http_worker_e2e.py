"""HTTP -> background Keddy/Judge -> PostgreSQL-like ledger regression.

All requests go through the real FastAPI routes and a disposable in-memory
SQLite database. Model calls are stubbed at the provider boundary only.
Real PostgreSQL locking / migrations are exercised by the separate CI job.
No local user, configured DATABASE_URL, OpenAI API key or browser is required.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import (
    Base, GameRun, PlatinumHelperCall, PlatinumHelperPurchase,
    Question, QuestionAppeal, QuestionAttempt, Topic, WalletTransaction,
)
from persistence.star_economy import star_balances


class AppealsHttpWorkerE2ETests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)

        self.enterContext(patch("persistence.appeal_worker.engine",
                                return_value=self.engine))
        self.enterContext(patch.dict(os.environ, {
            "AUTH_COOKIE_SECURE": "false",
            "QUIZ_COMPLAINT_CADDY_MODEL_ID": "gpt-6-luna",
            "QUIZ_COMPLAINT_CADDY_REASONING_EFFORT": "low",
            "QUIZ_COMPLAINT_CADDY_MAX_OUTPUT_TOKENS": "800",
            "QUIZ_COMPLAINT_CADDY_TIMEOUT_SECONDS": "12",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "gpt-6.1-sol",
            "QUIZ_COMPLAINT_JUDGE_REASONING_EFFORT": "medium",
            "QUIZ_COMPLAINT_JUDGE_MAX_OUTPUT_TOKENS": "1800",
            "QUIZ_COMPLAINT_JUDGE_TIMEOUT_SECONDS": "40",
            "QUIZ_QA_APPEAL_CREDITS_ENABLED": "false",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        registered = self.client.post("/api/auth/register", json={
            "username": "AppealHttpQA",
            "password": "offline-test-password-2026!",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        self.user_id = UUID(registered.json()["id"])

    @staticmethod
    def mock_model(messages, role, **kwargs):
        if role == "KEDDY":
            return json.dumps({
                "summary_en": "Conflicting award year and programming language.",
                "claims_to_check": ["1972 Turing Award recipient", "COBOL contributions"],
                "identified_issues": ["no_correct"],
                "needs_external_fact_check": True,
            })
        if role == "JUDGE":
            return json.dumps({
                "decision": "upheld",
                "explanation_en": "No option meets both claims.",
                "explanation_player_language": "Ни один вариант не отвечает обоим условиям.",
                "rationale_facts": ["The 1972 Turing Award was awarded to Dijkstra."],
            })
        raise AssertionError("Unexpected model stage")

    def fixture(self, number: int, *, before_answer: bool = False,
                answered_correct: bool = False):
        now = datetime.now(timezone.utc)
        status = ("completed" if number == 16 else "main") if answered_correct else (
            "main" if before_answer else "lost"
        )
        with Session(self.engine, expire_on_commit=False) as db:
            topic = Topic(
                slug="appeal-http-e2e-" + uuid4().hex[:12],
                labels={"ru": "Тест жалоб"},
            )
            db.add(topic)
            db.flush()
            run = GameRun(
                user_id=self.user_id,
                language="Russian",
                status=status,
                correct_main_answers=(min(number, 15) if answered_correct
                                      else 0 if before_answer else min(number - 1, 15)),
                bonus_wager_accepted=number == 16,
                bonus_correct=answered_correct if number == 16 else None,
                finished_at=now if status in ("lost", "completed") else None,
            )
            db.add(run)
            db.flush()
            question = Question(
                topic_id=topic.id,
                language="Russian",
                requested_difficulty=number - 1,
                text="Кто получил премию Тьюринга 1972 года за COBOL?",
                choices={
                    "A": "Джон Бэкус",
                    "B": "Грейс Хоппер",
                    "C": "Эдсгер Дейкстра",
                    "D": "Дональд Кнут",
                },
                correct_key="B",
                fingerprint=uuid4().hex + uuid4().hex,
                status="approved",
                generator_provider="openai",
                generator_model="gpt-5.4",
                prompt_version="test",
                schema_version="v1",
            )
            db.add(question)
            db.flush()
            attempt = QuestionAttempt(
                run_id=run.id,
                question_id=question.id,
                number=number,
                requested_difficulty=number - 1,
                answered_at=None if before_answer else now,
                selected_key=None if before_answer else "B" if answered_correct else "A",
                is_correct=None if before_answer else answered_correct,
                hints_used=1 if number == 10 else 0,
                appeal_expenses={"hint_bronze": 2} if number == 10 else {},
            )
            db.add(attempt)
            db.flush()
            balance = (
                {"bronze": 2} if before_answer else
                {"bronze": 4, "silver": 4}
                if number == 10 or (answered_correct and 6 <= number <= 10) else
                {"gold": 4}
            )
            for tier, amount in balance.items():
                db.add(WalletTransaction(
                    user_id=self.user_id,
                    run_id=run.id,
                    idempotency_key=f"qa-http:{run.id}:{tier}:seed",
                    amount=amount,
                    reason=f"star/{tier}/qa-http-seed",
                ))
            if number == 16:
                db.add(WalletTransaction(
                    user_id=self.user_id,
                    run_id=run.id,
                    idempotency_key=f"qa-http:{run.id}:bonus-wager",
                    amount=-1,
                    reason="star/gold/qa-http-bonus-wager",
                ))
            db.commit()
            return str(run.id), str(question.id), str(attempt.id)

    def appeal(self, run_id, question_id, *, reason="no_correct", request_id=None):
        payload = {
            "question_id": question_id,
            "request_id": request_id or str(uuid4()),
            "reasons": [reason],
            "comment": "В вопросе объединены несовместимые факты.",
            "source_links": [],
            "confirm_nonrefundable_fee": True,
            "expected_fee_bronze": 0,
        }
        return self.client.post(f"/api/v2/games/{run_id}/appeals", json=payload), payload

    def ledger(self, appeal_id):
        with Session(self.engine) as db:
            transactions = db.scalars(select(WalletTransaction).where(
                WalletTransaction.idempotency_key.like(f"appeal:{appeal_id}:%")
            )).all()
            return {
                t.idempotency_key.split(":")[-1]: (t.reason.split("/")[1], t.amount)
                for t in transactions
            }

    def test_q10_http_to_worker_history_and_exactly_once_silver_payout(self):
        run_id, question_id, _ = self.fixture(10)
        quote = self.client.get(
            f"/api/v2/games/{run_id}/appeals/{question_id}/quote")
        self.assertEqual(quote.status_code, 200, quote.text)
        self.assertEqual(quote.json()["deposit_tier"], "silver")
        self.assertEqual(quote.json()["hint_refund_bronze_if_upheld"], 2)
        self.assertFalse(quote.json()["before_answer"])

        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model) as calls:
            submitted, payload = self.appeal(run_id, question_id)
        self.assertEqual(submitted.status_code, 201, submitted.text)
        appeal_id = submitted.json()["id"]
        self.assertEqual(calls.call_count, 2)  # one Keddy, one independent Judge

        history = self.client.get("/api/v2/appeals")
        self.assertEqual(history.status_code, 200, history.text)
        case = next(i for i in history.json() if i["id"] == appeal_id)
        self.assertEqual(case["status"], "upheld")
        self.assertEqual(case["answer_key"], "B")
        self.assertEqual(case["compensation"], {"tier": "silver", "amount": 1})
        self.assertTrue(case["deposit"]["refunded"])

        expected = {
            "deposit": ("silver", -1),
            "appeal-deposit-refund": ("silver", 1),
            "appeal-hint-refund": ("bronze", 2),
            "appeal-compensation": ("silver", 1),
        }
        self.assertEqual(self.ledger(appeal_id), expected)
        with Session(self.engine) as db:
            self.assertEqual(star_balances(db, self.user_id)["bronze"], 6)
            self.assertEqual(star_balances(db, self.user_id)["silver"], 5)
            self.assertEqual(db.get(Question, UUID(question_id)).status, "rejected")
            self.assertEqual(db.get(GameRun, UUID(run_id)).status, "lost")

        # Same request ID is idempotent even after background adjudication.
        duplicate = self.client.post(
            f"/api/v2/games/{run_id}/appeals", json=payload)
        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        self.assertEqual(duplicate.json()["id"], appeal_id)
        self.assertEqual(self.ledger(appeal_id), expected)
        payload["request_id"] = str(uuid4())
        rejected = self.client.post(
            f"/api/v2/games/{run_id}/appeals", json=payload)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(self.ledger(appeal_id), expected)

        other = TestClient(app)
        self.addCleanup(other.close)
        registered = other.post("/api/auth/register", json={
            "username": "AppealOtherQA", "password": "offline-test-password-2026!",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertEqual(other.get("/api/v2/appeals").json(), [])
        self.assertEqual(other.get(f"/api/v2/appeals/{appeal_id}").status_code, 404)

    def test_q16_http_worker_refunds_wager_and_awards_platinum(self):
        run_id, question_id, _ = self.fixture(16)
        quote = self.client.get(
            f"/api/v2/games/{run_id}/appeals/{question_id}/quote")
        self.assertEqual(quote.status_code, 200, quote.text)
        self.assertEqual(quote.json()["deposit_tier"], "gold")
        self.assertTrue(quote.json()["bonus_wager_refund_if_upheld"])

        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model) as calls:
            response, _ = self.appeal(run_id, question_id)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(calls.call_count, 2)
        appeal_id = response.json()["id"]
        self.assertEqual(self.ledger(appeal_id), {
            "deposit": ("gold", -1),
            "appeal-deposit-refund": ("gold", 1),
            "appeal-compensation": ("platinum", 1),
            "appeal-bonus-wager-refund": ("gold", 1),
        })
        with Session(self.engine) as db:
            balances = star_balances(db, self.user_id)
            self.assertEqual(balances["gold"], 4)
            self.assertEqual(balances["platinum"], 1)
            self.assertEqual(db.get(GameRun, UUID(run_id)).status, "lost")
        case = self.client.get(f"/api/v2/appeals/{appeal_id}").json()
        self.assertEqual(case["status"], "upheld")
        self.assertTrue(case["bonus_wager_refunded"])

    def test_preanswer_key_stays_private_and_no_loss_compensation(self):
        run_id, question_id, attempt_id = self.fixture(1, before_answer=True)
        quote = self.client.get(
            f"/api/v2/games/{run_id}/appeals/{question_id}/quote")
        self.assertEqual(quote.status_code, 200, quote.text)
        self.assertTrue(quote.json()["before_answer"])
        self.assertNotIn("correct_key", quote.text)
        invalid, _ = self.appeal(run_id, question_id, reason="incorrect_key")
        self.assertEqual(invalid.status_code, 409, invalid.text)

        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model) as calls:
            submitted, _ = self.appeal(run_id, question_id)
        self.assertEqual(submitted.status_code, 201, submitted.text)
        self.assertEqual(calls.call_count, 2)
        appeal_id = submitted.json()["id"]
        case = self.client.get(f"/api/v2/appeals/{appeal_id}").json()
        self.assertEqual(case["status"], "upheld")
        self.assertIsNone(case["answer_key"])
        self.assertIsNone(case["verdict"], "Judge explanation must not spoil the answer")
        self.assertNotIn("correct_key", json.dumps(case))
        self.assertEqual(case["compensation"]["amount"], 0)
        listing = self.client.get("/api/v2/appeals")
        self.assertEqual(listing.status_code, 200, listing.text)
        listed = next(item for item in listing.json() if item["id"] == appeal_id)
        self.assertIsNone(listed["verdict"])
        self.assertIsNone(listed["answer_key"])
        with Session(self.engine) as db:
            original = db.get(QuestionAppeal, UUID(appeal_id))
            saved_verdict = original.verdict_localized
            self.assertTrue(saved_verdict)
            # Simulate the natural end of this same round; do not settle twice.
            run = db.get(GameRun, UUID(run_id))
            run.status = "completed"
            db.commit()
        after = self.client.get(f"/api/v2/appeals/{appeal_id}")
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["answer_key"], "B")
        self.assertEqual(after.json()["verdict"], saved_verdict)
        listed_after = next(item for item in
                            self.client.get("/api/v2/appeals").json()
                            if item["id"] == appeal_id)
        self.assertEqual(listed_after["verdict"], saved_verdict)
        self.assertEqual(self.ledger(appeal_id), {
            "deposit": ("bronze", -1),
            "appeal-deposit-refund": ("bronze", 1),
        })
        with Session(self.engine) as db:
            self.assertEqual(star_balances(db, self.user_id)["bronze"], 2)
            self.assertEqual(db.get(GameRun, UUID(run_id)).status, "main")
            self.assertIsNotNone(db.get(
                QuestionAttempt, UUID(attempt_id)).superseded_at)


    def test_correct_answer_can_be_appealed_without_score_or_loss_compensation(self):
        run_id, question_id, attempt_id = self.fixture(6, answered_correct=True)
        quote = self.client.get(
            f"/api/v2/games/{run_id}/appeals/{question_id}/quote")
        self.assertEqual(quote.status_code, 200, quote.text)
        self.assertFalse(quote.json()["before_answer"])
        self.assertEqual(quote.json()["deposit_tier"], "silver")
        self.assertIsNone(quote.json()["loss_compensation_tier_if_upheld"])
        self.assertFalse(quote.json()["bonus_wager_refund_if_upheld"])

        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model) as calls:
            submitted, payload = self.appeal(run_id, question_id)
        self.assertEqual(submitted.status_code, 201, submitted.text)
        self.assertEqual(calls.call_count, 2)
        appeal_id = submitted.json()["id"]
        appeal = self.client.get(f"/api/v2/appeals/{appeal_id}").json()
        self.assertEqual(appeal["status"], "upheld")
        self.assertEqual(appeal["selected_key"], "B")
        self.assertEqual(appeal["answer_key"], "B")
        self.assertEqual(appeal["compensation"]["amount"], 0)
        self.assertEqual(self.ledger(appeal_id), {
            "deposit": ("silver", -1),
            "appeal-deposit-refund": ("silver", 1),
        })
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(run_id))
            attempt = db.get(QuestionAttempt, UUID(attempt_id))
            self.assertEqual(run.status, "main")
            self.assertEqual(run.correct_main_answers, 6)
            self.assertTrue(attempt.is_correct)
            self.assertEqual(attempt.selected_key, "B")
            self.assertIsNone(attempt.superseded_at)
            self.assertEqual(star_balances(db, self.user_id)["silver"], 4)

        duplicate = self.client.post(
            f"/api/v2/games/{run_id}/appeals", json=payload)
        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        payload["request_id"] = str(uuid4())
        self.assertEqual(self.client.post(
            f"/api/v2/games/{run_id}/appeals", json=payload).status_code, 409)

    def test_correct_bonus_answer_never_refunds_wager_or_awards_loss_compensation(self):
        run_id, question_id, attempt_id = self.fixture(16, answered_correct=True)
        quote = self.client.get(
            f"/api/v2/games/{run_id}/appeals/{question_id}/quote")
        self.assertEqual(quote.status_code, 200, quote.text)
        self.assertIsNone(quote.json()["loss_compensation_tier_if_upheld"])
        self.assertFalse(quote.json()["bonus_wager_refund_if_upheld"])

        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model):
            submitted, _ = self.appeal(run_id, question_id)
        self.assertEqual(submitted.status_code, 201, submitted.text)
        appeal = self.client.get(f"/api/v2/appeals/{submitted.json()['id']}").json()
        self.assertEqual(appeal["status"], "upheld")
        self.assertFalse(appeal["bonus_wager_refunded"])
        self.assertEqual(appeal["compensation"]["amount"], 0)
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(run_id))
            attempt = db.get(QuestionAttempt, UUID(attempt_id))
            self.assertEqual(run.status, "completed")
            self.assertTrue(attempt.is_correct)
            self.assertIsNone(attempt.superseded_at)

    def paid_helper(self, run_id: str, attempt_id: str, *,
                    price: int = 2, consulted: bool = False,
                    hint_bronze: int = 0):
        """Record actual purchase and (optionally) expenses before filing."""
        with Session(self.engine, expire_on_commit=False) as db:
            purchase = PlatinumHelperPurchase(
                run_id=UUID(run_id), user_id=self.user_id,
                price=price, profession="Historian", profile={"featured_high": [],
                "featured_low": [], "erudition": 50},
                consultations_used=int(consulted), refunded=False,
            )
            db.add(purchase)
            db.flush()
            db.add(WalletTransaction(
                user_id=self.user_id, run_id=UUID(run_id),
                idempotency_key=f"qa-http:{run_id}:platinum-seed",
                amount=price, reason="star/platinum/qa-http-seed",
            ))
            db.add(WalletTransaction(
                user_id=self.user_id, run_id=UUID(run_id),
                idempotency_key=f"helper:{purchase.id}:purchase",
                amount=-price, reason="star/platinum/helper-purchase",
            ))
            if consulted:
                db.add(PlatinumHelperCall(
                    purchase_id=purchase.id, request_key=str(uuid4()),
                    question_attempt_id=UUID(attempt_id), topic="General knowledge",
                    outcome="KNOW", selected_key="B", status="completed",
                    response_text="The answer is B.",
                ))
            if hint_bronze:
                attempt = db.get(QuestionAttempt, UUID(attempt_id))
                attempt.hints_used = 1
                attempt.appeal_expenses = {"hint_bronze": hint_bronze}
                db.add(WalletTransaction(
                    user_id=self.user_id, run_id=UUID(run_id),
                    idempotency_key=f"qa-http:{run_id}:hint-spent",
                    amount=-hint_bronze, reason="star/bronze/hint-purchase",
                ))
            db.commit()
            return str(purchase.id)

    def test_unused_helper_not_refunded_while_game_continues(self):
        # One upheld pre-answer complaint must not take away a usable helper.
        run_id, question_id, attempt_id = self.fixture(12, before_answer=True)
        purchase_id = self.paid_helper(run_id, attempt_id, price=2)
        with Session(self.engine) as db:
            db.add(WalletTransaction(
                user_id=self.user_id, run_id=UUID(run_id),
                idempotency_key=f"qa-http:{run_id}:gold-seed",
                amount=2, reason="star/gold/qa-http-seed",
            ))
            db.commit()
        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model):
            submission, _ = self.appeal(run_id, question_id)
        self.assertEqual(submission.status_code, 201, submission.text)
        appeal_id = submission.json()["id"]
        self.assertEqual(self.client.get(
            f"/api/v2/appeals/{appeal_id}").json()["status"], "upheld")
        self.assertEqual(self.ledger(appeal_id), {
            "deposit": ("gold", -1), "appeal-deposit-refund": ("gold", 1),
        })
        with Session(self.engine) as db:
            purchase = db.get(PlatinumHelperPurchase, UUID(purchase_id))
            self.assertFalse(purchase.refunded)
            self.assertEqual(purchase.consultations_used, 0)
            self.assertEqual(db.get(GameRun, UUID(run_id)).status, "main")
            self.assertEqual(star_balances(db, self.user_id)["platinum"], 0)
            self.assertEqual(star_balances(db, self.user_id)["gold"], 2)
            self.assertIsNone(db.scalar(select(WalletTransaction.id).where(
                WalletTransaction.idempotency_key == f"helper:{purchase_id}:refund"
            )))

    def test_unused_helper_refunded_once_only_when_bad_question_caused_loss(self):
        run_id, question_id, attempt_id = self.fixture(12)
        purchase_id = self.paid_helper(run_id, attempt_id, price=2, hint_bronze=2)
        # A separate hint refund is flushed before the unused-helper receipt.
        with Session(self.engine) as db:
            db.add(WalletTransaction(
                user_id=self.user_id, run_id=UUID(run_id),
                idempotency_key=f"qa-http:{run_id}:bronze-seed",
                amount=2, reason="star/bronze/qa-http-seed",
            ))
            db.commit()
        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model):
            submission, payload = self.appeal(run_id, question_id)
        self.assertEqual(submission.status_code, 201, submission.text)
        appeal_id = submission.json()["id"]
        self.assertEqual(self.client.get(
            f"/api/v2/appeals/{appeal_id}").json()["status"], "upheld")
        self.assertEqual(self.ledger(appeal_id), {
            "deposit": ("gold", -1),
            "appeal-deposit-refund": ("gold", 1),
            "appeal-hint-refund": ("bronze", 2),
            "appeal-compensation": ("gold", 1),
        })
        self.assertEqual(
            self.client.get(f"/api/v2/appeals/{appeal_id}").json()["expenses_refunded"],
            [{"tier": "bronze", "amount": 2, "reason": "hints"},
             {"tier": "platinum", "amount": 2, "reason": "unused_helper"}],
            "Unused-helper platinum must survive JSON persistence after hints",
        )
        with Session(self.engine) as db:
            purchase = db.get(PlatinumHelperPurchase, UUID(purchase_id))
            self.assertTrue(purchase.refunded)
            self.assertEqual(purchase.consultations_used, 0)
            self.assertEqual(star_balances(db, self.user_id)["platinum"], 2)
            self.assertEqual(star_balances(db, self.user_id)["gold"], 5)
            self.assertEqual(db.scalar(select(WalletTransaction.amount).where(
                WalletTransaction.idempotency_key == f"helper:{purchase_id}:refund"
            )), 2)
        # Repeated HTTP request and worker settlement never refund twice.
        repeat = self.client.post(
            f"/api/v2/games/{run_id}/appeals", json=payload)
        self.assertEqual(repeat.status_code, 201, repeat.text)
        self.assertEqual(repeat.json()["id"], appeal_id)
        with Session(self.engine) as db:
            self.assertEqual(star_balances(db, self.user_id)["platinum"], 2)
            self.assertEqual(db.scalar(select(WalletTransaction.amount).where(
                WalletTransaction.idempotency_key == f"helper:{purchase_id}:refund"
            )), 2)

    def test_preanswer_hint_and_helper_consultation_both_compensated(self):
        # Q12: -3 bronze hint, paid platinum helper consultation, -1 gold
        # appeal deposit, then uphold: +3 bronze, +1 gold deposit +1 helper
        # gold. No "lost game" compensation for a pre-answer complaint.
        run_id, question_id, attempt_id = self.fixture(12, before_answer=True)
        purchase_id = self.paid_helper(
            run_id, attempt_id, price=2, consulted=True, hint_bronze=3,
        )
        with Session(self.engine) as db:
            db.add(WalletTransaction(
                user_id=self.user_id, run_id=UUID(run_id),
                idempotency_key=f"qa-http:{run_id}:gold-seed",
                amount=2, reason="star/gold/qa-http-seed",
            ))
            db.add(WalletTransaction(
                user_id=self.user_id, run_id=UUID(run_id),
                idempotency_key=f"qa-http:{run_id}:bronze-extra-seed",
                amount=1, reason="star/bronze/qa-http-seed",
            ))
            db.commit()
        with patch("persistence.appeal_worker._completion",
                   side_effect=self.mock_model):
            submission, _ = self.appeal(run_id, question_id)
        self.assertEqual(submission.status_code, 201, submission.text)
        appeal_id = submission.json()["id"]
        case = self.client.get(f"/api/v2/appeals/{appeal_id}").json()
        self.assertEqual(case["status"], "upheld")
        self.assertEqual(case["compensation"]["amount"], 0)
        self.assertEqual(case["expenses_refunded"], [
            {"tier": "bronze", "amount": 3, "reason": "hints"},
            {"tier": "gold", "amount": 1, "reason": "helper"},
        ], "All credits must survive fresh API/DB reads, not just wallet writes")
        self.assertEqual(self.ledger(appeal_id), {
            "deposit": ("gold", -1),
            "appeal-deposit-refund": ("gold", 1),
            "appeal-hint-refund": ("bronze", 3),
        })
        with Session(self.engine) as db:
            purchase = db.get(PlatinumHelperPurchase, UUID(purchase_id))
            self.assertFalse(purchase.refunded)
            self.assertEqual(purchase.consultations_used, 1)
            self.assertEqual(star_balances(db, self.user_id)["bronze"], 3)
            self.assertEqual(star_balances(db, self.user_id)["gold"], 3)
            self.assertEqual(star_balances(db, self.user_id)["platinum"], 0)
            self.assertEqual(db.scalar(select(WalletTransaction.amount).where(
                WalletTransaction.idempotency_key == f"helper:{purchase_id}:appeal-gold"
            )), 1)
            self.assertIsNone(db.scalar(select(WalletTransaction.id).where(
                WalletTransaction.idempotency_key == f"helper:{purchase_id}:refund"
            )))

        # Reconstruct a historical receipt affected by the original in-place
        # JSON mutation. The gold transaction remains and must never replay.
        from scripts.repair_appeal_helper_receipt import repair
        with Session(self.engine) as db:
            row = db.get(QuestionAppeal, UUID(appeal_id))
            row.verdict_data = {
                **(row.verdict_data or {}),
                "expenses_refunded": [{"tier": "bronze", "amount": 3, "reason": "hints"}],
            }
            db.commit()
        with Session(self.engine) as db:
            preview = repair(db, UUID(appeal_id))
            self.assertEqual(preview["status"], "eligible_dry_run")
            db.rollback()
        self.assertEqual(self.client.get(
            f"/api/v2/appeals/{appeal_id}").json()["expenses_refunded"],
            [{"tier": "bronze", "amount": 3, "reason": "hints"}])
        with Session(self.engine) as db:
            fixed = repair(db, UUID(appeal_id), apply=True)
            self.assertEqual(fixed["status"], "repaired")
            db.commit()
        with Session(self.engine) as db:
            second = repair(db, UUID(appeal_id), apply=True)
            self.assertEqual(second["status"], "already_recorded")
            db.rollback()
        self.assertEqual(self.client.get(
            f"/api/v2/appeals/{appeal_id}").json()["expenses_refunded"], [
            {"tier": "bronze", "amount": 3, "reason": "hints"},
            {"tier": "gold", "amount": 1, "reason": "helper"},
        ])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(WalletTransaction.amount).where(
                WalletTransaction.idempotency_key == f"helper:{purchase_id}:appeal-gold"
            )), 1)
            self.assertEqual(star_balances(db, self.user_id)["gold"], 3)


if __name__ == "__main__":
    unittest.main()
