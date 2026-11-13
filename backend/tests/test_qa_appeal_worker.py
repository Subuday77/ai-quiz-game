"""Offline Q10/Q16 real-worker accounting QA: no external AI requests."""
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from persistence.models import Base, Question, Topic
from scripts import qa_appeal_worker as qa


class IsolatedAppealWorkerTests(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.eng, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.eng)
        self.addCleanup(self.eng.dispose)
        self.enterContext(patch.dict(os.environ, {
            qa.ENABLE_FLAG: "true",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "gpt-6.1-sol",
        }))
        self.enterContext(patch("scripts.qa_appeal_worker.engine", return_value=self.eng))
        self.enterContext(patch("persistence.appeal_worker.engine", return_value=self.eng))
        self.enterContext(patch("persistence.appeal_worker._config",
                                side_effect=lambda role: (
                                    "gpt-6.1-sol" if role == "JUDGE" else "gpt-6-luna",
                                    "low", 1800, 40.0)))
        def mock_completion(_messages, role, **_kwargs):
            if role == "KEDDY":
                return json.dumps({
                    "summary_en": "Award-year and COBOL conflict.",
                    "claims_to_check": ["1972 Turing award recipient"],
                    "identified_issues": ["no_correct"],
                    "needs_external_fact_check": True,
                })
            if role == "JUDGE":
                return json.dumps({
                    "decision": "upheld",
                    "explanation_en": "No option satisfies both clauses.",
                    "explanation_player_language": "Нет правильного ответа.",
                    "rationale_facts": ["Dijkstra won in 1972, not for COBOL."],
                })
            raise AssertionError("Unexpected model role")
        self.completions = self.enterContext(patch(
            "persistence.appeal_worker._completion",
            side_effect=mock_completion,
        ))
        with Session(self.eng) as db:
            topic = Topic(slug="known-qa-source", labels={"ru": "Программирование"})
            db.add(topic)
            db.flush()
            source = Question(
                topic_id=topic.id, language="Russian",
                requested_difficulty=9,
                text="Какой учёный получил Премию Тьюринга 1972 года "
                     "за вклад в создание и развитие языка COBOL?",
                choices={"A": "Джон Бэкус", "B": "Грейс Хоппер",
                         "C": "Эдсгер Дейкстра", "D": "Дональд Кнут"},
                correct_key="B", status="quarantined",
                fingerprint=uuid4().hex + uuid4().hex,
                generator_provider="openai", generator_model="gpt-5.4",
                prompt_version="v2", schema_version="v1",
            )
            db.add(source)
            db.commit()
            self.source_id = source.id

    def test_q10_worker_refunds_silver_deposit_and_bronze_hints_once(self):
        prepared = qa.prepare(scenario="q10", source_question_id=self.source_id,
                              confirm=qa.CONFIRM_TEXT)
        self.assertEqual(prepared["appeal_status"], "pending")
        self.assertEqual(prepared["after_filing"]["silver"], 3)
        result = qa.run_worker(appeal_id=qa.UUID(prepared["appeal_id"]),
                               confirm=qa.CONFIRM_TEXT)
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["idempotency"]["no_extra_credits"])
        self.assertEqual(result["status"], "upheld")
        self.assertEqual(result["actual_balances"]["bronze"], 6)
        self.assertEqual(result["actual_balances"]["silver"], 5)
        self.assertEqual(self.completions.call_count, 2)
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.source_id).status, "quarantined")

    def test_q16_worker_refunds_gold_deposit_wager_and_platinum_once(self):
        prepared = qa.prepare(scenario="q16", source_question_id=self.source_id,
                              confirm=qa.CONFIRM_TEXT)
        self.assertEqual(prepared["after_filing"]["gold"], 2)
        result = qa.run_worker(appeal_id=qa.UUID(prepared["appeal_id"]),
                               confirm=qa.CONFIRM_TEXT)
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["idempotency"]["no_extra_credits"])
        self.assertEqual(result["actual_balances"]["gold"], 4)
        self.assertEqual(result["actual_balances"]["platinum"], 1)
        self.assertEqual(self.completions.call_count, 2)
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.source_id).status, "quarantined")

    def test_write_guards_and_non_qa_ids(self):
        with patch.dict(os.environ, {qa.ENABLE_FLAG: "false"}):
            with self.assertRaisesRegex(ValueError, "Write operation requires"):
                qa.prepare(scenario="q10", source_question_id=self.source_id,
                           confirm=qa.CONFIRM_TEXT)
        with self.assertRaisesRegex(ValueError, "Write operation requires"):
            qa.prepare(scenario="q10", source_question_id=self.source_id,
                       confirm="")
        with self.assertRaisesRegex(ValueError, "QA appeal not found"):
            qa.run_worker(appeal_id=uuid4(), confirm=qa.CONFIRM_TEXT)
        self.completions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
