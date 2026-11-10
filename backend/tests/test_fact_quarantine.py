"""2026-11-02 Turing false-premise incident: deterministic offline regressions.

No network, provider, or PostgreSQL instance is needed.
"""
import hashlib
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import generate_valid_question
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, QuestionFact, QuestionReview,
    Topic, User,
)
from persistence.quarantine_known_bad import (
    TARGET_STEM, quarantine_known_bad,
)
from tagging.contract import TaggedQuestion
from tagging.quality_skill.engine import evaluate_shadow
from tagging.quality_skill.fact_tool import known_false_fact, known_false_triple

WRONG = {
    "difficulty": 9,
    "question": TARGET_STEM,
    "answers": {
        "A": "Джон Бэкус", "B": "Грейс Хоппер",
        "C": "Эдсгер Дейкстра", "D": "Дональд Кнут",
    },
    "correct_answer": "B",
    "tags": {
        "entity": ["grace-hopper", "cobol", "turing-award"],
        "concept": ["programming-languages", "computer-science-awards",
                    "software-history"],
        "keyword": ["cobol", "turing-award", "compiler", "navy"],
    },
    "fact": {
        "subject": "grace-hopper", "relation": "turing-award-year", "object": "1972",
    },
}


def wrong_question():
    return TaggedQuestion.model_validate(WRONG)


class FactToolRegressions(unittest.TestCase):
    def test_exact_documented_false_fact_is_source_backed(self):
        fact = known_false_fact(wrong_question())
        self.assertIsNotNone(fact)
        self.assertEqual(fact["verified_recipient"], "edsger-w-dijkstra")
        self.assertIn("amturing.acm.org", fact["reference"])
        self.assertIsNone(known_false_triple(
            "edsger-w-dijkstra", "turing-award-year", "1972",
        ))
        self.assertIsNone(known_false_triple(
            "grace-hopper", "turing-award-year", "1971",
        ))
        self.assertIsNone(known_false_triple(
            "grace-hopper", "cobol-contributions", "true",
        ))

    def test_compound_award_year_reason_is_nominated_for_verification(self):
        result = evaluate_shadow(wrong_question(), requested_difficulty=9)
        codes = {f["code"] for f in result["findings"]}
        self.assertIn("needs_source_verification:compound_award_year_reason", codes)
        self.assertIn("known_contradiction:acm_turing_1972_false_recipient", codes)
        self.assertTrue(result["would_arbitrate"])
        self.assertFalse(result["arbiter_called"])
        self.assertEqual(result["decision"], "observe_only")

    def test_uncurated_award_year_risk_never_claims_false(self):
        candidate = wrong_question().model_copy(deep=True)
        candidate.fact.subject = "edsger-w-dijkstra"
        candidate.tags.entity[0] = "edsger-w-dijkstra"
        result = evaluate_shadow(candidate, requested_difficulty=9)
        compound = [f for f in result["findings"]
                    if f["code"].endswith("compound_award_year_reason")]
        self.assertEqual(len(compound), 1)
        self.assertEqual(compound[0]["evidence"]["claim_truth"], "unknown")
        self.assertEqual(compound[0]["evidence"]["verification"], "not_performed")
        self.assertFalse([f for f in result["findings"]
                          if f["code"].startswith("known_contradiction:")])

    def test_no_arbitration_for_plain_authorship(self):
        q = wrong_question().model_copy(deep=True)
        q.question = "Кто написал пьесу «Синяя птица»?"
        q.fact.subject, q.fact.relation, q.fact.object = (
            "the-blue-bird", "written-by", "maurice-maeterlinck"
        )
        result = evaluate_shadow(q, requested_difficulty=9)
        self.assertFalse([f for f in result["findings"] if f["tool"] == "fact"])

    def test_bad_fact_is_rejected_before_paid_critic(self):
        with patch.dict(os.environ, {
            "AI_GENERATION_MAX_ATTEMPTS": "1",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12",
            "AI_QUALITY_MODE": "enforce",
            "AI_LOCAL_LEAK_MODE": "off",
        }), patch("api.create_question", return_value=WRONG) as gen, patch(
            "tagging.review.check_tagged_quality"
        ) as critic:
            with self.assertRaises(HTTPException) as result:
                generate_valid_question(
                    difficulty=9, language="Russian", category="Technology",
                    tag_history=[], tag_context=[],
                )
        self.assertEqual(result.exception.status_code, 502)
        gen.assert_called_once()
        critic.assert_not_called()


class ExactQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
        user = User(username="AI_Tester", username_key="ai_tester")
        topic = Topic(slug="technology", labels={"en": "Technology"})
        self.db.add_all([user, topic])
        self.db.flush()
        self.user_id = user.id
        self.topic_id = topic.id
        run = GameRun(user_id=user.id, language="Russian",
                      status="lost", correct_main_answers=9)
        self.db.add(run)
        self.db.flush()
        question = Question(
            topic_id=topic.id, language="Russian", requested_difficulty=9,
            text=TARGET_STEM, choices=WRONG["answers"], correct_key="B",
            fingerprint=hashlib.sha256(TARGET_STEM.encode()).hexdigest(),
            status="approved", generator_provider="openai",
            generator_model="gpt-5.4", prompt_version="tagged-v2",
            schema_version="question_tagged_v2",
        )
        self.db.add(question)
        self.db.flush()
        self.qid = question.id
        self.db.add_all([
            QuestionFact(
                question_id=question.id,
                fact_key="f" * 64, subject="grace-hopper",
                relation="turing-award-year", object="1972",
                origin="generator",
            ),
            QuestionReview(
                question_id=question.id, checker_type="ai_tagged_critic",
                contract_version="tagged_review_v5", decision="accept",
                issues=[], details={"assessment": {"total": 0}},
            ),
            QuestionAttempt(
                run_id=run.id, question_id=question.id, number=10,
                requested_difficulty=9,
                answered_at=datetime.now(timezone.utc),
                selected_key="C", is_correct=False,
            ),
        ])
        self.db.commit()

    def test_dry_run_then_apply_then_idempotent_and_audited(self):
        self.assertEqual(quarantine_known_bad(self.db)["changed"], 1)
        self.assertEqual(self.db.get(Question, self.qid).status, "approved")
        applied = quarantine_known_bad(self.db, apply=True)
        self.assertEqual((applied["matched"], applied["changed"]), (1, 1))
        self.assertEqual(self.db.get(Question, self.qid).status, "quarantined")
        self.assertEqual(quarantine_known_bad(self.db, apply=True)["changed"], 0)
        checks = self.db.scalars(
            select(QuestionReview).where(QuestionReview.question_id == self.qid)
        ).all()
        self.assertEqual(len(checks), 2)
        self.assertEqual({x.decision for x in checks}, {"accept", "quarantine"})
        self.assertEqual(self.db.scalar(select(func.count(QuestionAttempt.id))), 1)
        attempt = self.db.scalar(select(QuestionAttempt))
        self.assertFalse(attempt.is_correct)
        self.assertEqual(attempt.selected_key, "C")
        self.assertEqual(self.db.scalar(select(func.count(GameRun.id))), 1)

    def test_wrong_metadata_aborts_without_changing_anything(self):
        fact = self.db.get(QuestionFact, self.qid)
        fact.object = "1971"
        self.db.commit()
        with self.assertRaises(ValueError):
            quarantine_known_bad(self.db, apply=True)
        self.assertEqual(self.db.get(Question, self.qid).status, "approved")
        self.assertEqual(self.db.scalar(select(func.count(QuestionReview.id))), 1)

    def test_no_match_is_safe(self):
        q = self.db.get(Question, self.qid)
        q.text = "Completely unrelated question"
        self.db.commit()
        self.assertEqual(quarantine_known_bad(self.db, apply=True)["matched"], 0)
        self.assertEqual(self.db.get(Question, self.qid).status, "approved")


if __name__ == "__main__":
    unittest.main()
