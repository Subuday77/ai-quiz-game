"""Offline votes, hidden tally, ownership, threshold and Caddy/Judge review."""
import json
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, QuestionModeration,
    QuestionReview, QuestionVote, QuestionVoteReview, Topic, WalletTransaction,
)
from persistence.question_source_policy import (
    community_vote_threshold, question_source_order,
)
from persistence.vote_worker import process_vote_review


class VoteConfigTests(unittest.TestCase):
    def test_source_order_and_negative_threshold_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(question_source_order(), "generate_first")
            self.assertEqual(community_vote_threshold(), -12)
        with patch.dict(os.environ, {"QUIZ_QUESTION_SOURCE_ORDER": "pool_first",
                                    "QUIZ_COMMUNITY_VOTE_THRESHOLD": "-15"}):
            self.assertEqual(question_source_order(), "pool_first")
            self.assertEqual(community_vote_threshold(), -15)
        for text in ("none", "", "pool", "random"):
            with self.subTest(text=text), patch.dict(
                    os.environ, {"QUIZ_QUESTION_SOURCE_ORDER": text}):
                with self.assertRaises(ValueError):
                    question_source_order()
        for text in ("0", "2", "abc", "-100001", "2.5"):
            with self.subTest(text=text), patch.dict(
                    os.environ, {"QUIZ_COMMUNITY_VOTE_THRESHOLD": text}):
                with self.assertRaises(ValueError):
                    community_vote_threshold()


class QuestionVotesTests(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.eng, "connect")
        def enable_fks(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.eng)
        self.addCleanup(self.eng.dispose)

        def override_db():
            with Session(self.eng, autoflush=False, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.enterContext(patch.dict(os.environ, {
            "AUTH_COOKIE_SECURE": "false",
            "QUIZ_COMMUNITY_VOTE_THRESHOLD": "-2",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "gpt-6.1-sol",
        }))
        self.review_task = self.enterContext(
            patch("persistence.vote_routes.process_vote_review"))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.user1 = self.register(self.client, "VotingAlice")
        with Session(self.eng, expire_on_commit=False) as db:
            topic = Topic(slug="vote-fixture-science", labels={"en": "Science"})
            db.add(topic)
            db.flush()
            q = Question(
                topic_id=topic.id, language="English", requested_difficulty=0,
                text="Which organ produces adrenaline?",
                choices={"A": "Thyroid", "B": "Adrenal gland",
                         "C": "Liver", "D": "Pancreas"},
                correct_key="B", fingerprint=uuid4().hex + uuid4().hex,
                status="approved", generator_provider="openai",
                generator_model="gpt-5.4", prompt_version="qa",
                schema_version="question_tagged_v3",
            )
            db.add(q)
            db.commit()
            self.qid = q.id
        self.run1 = self.add_attempt(self.user1)

    @staticmethod
    def register(client, username):
        result = client.post("/api/auth/register", json={
            "username": username, "password": "offline-test-password-2026!",
        })
        assert result.status_code == 201, result.text
        return UUID(result.json()["id"])

    def add_attempt(self, user_id):
        with Session(self.eng, expire_on_commit=False) as db:
            run = GameRun(user_id=user_id, language="English", status="main")
            db.add(run)
            db.flush()
            db.add(QuestionAttempt(
                run_id=run.id, question_id=self.qid,
                number=1, requested_difficulty=0,
            ))
            db.commit()
            return run.id

    def url(self, run):
        return f"/api/v2/games/{run}/questions/{self.qid}/vote"

    def test_toggle_change_and_f5_return_own_ballot_only(self):
        url = self.url(self.run1)
        self.assertEqual(self.client.put(url, json={"value": 1}).json(), {"my_vote": 1})
        self.assertEqual(self.client.put(url, json={"value": 1}).json(), {"my_vote": 1})
        self.assertEqual(self.client.put(url, json={"value": -1}).json(), {"my_vote": -1})
        view = self.client.get(f"/api/v2/games/{self.run1}/question")
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual(view.json()["my_vote"], -1)
        self.assertNotIn("vote_score", view.json())
        self.assertNotIn("correct_answer", view.json())
        self.assertEqual(self.client.put(url, json={"value": 0}).json(), {"my_vote": 0})
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.qid).vote_score, 0)
            self.assertEqual(db.scalar(select(func.count(QuestionVote.question_id))), 0)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)
        self.review_task.assert_not_called()

    def test_threshold_once_and_independent_player_votes(self):
        self.assertEqual(self.client.put(self.url(self.run1), json={"value": -1}).status_code, 200)
        other = TestClient(app)
        self.addCleanup(other.close)
        bob = self.register(other, "VotingBob")
        run2 = self.add_attempt(bob)
        self.assertEqual(other.put(self.url(run2), json={"value": -1}).status_code, 200)
        self.review_task.assert_called_once_with(self.qid)
        # No repeated queue entries, nor a second debit, if a ballot is changed.
        self.assertEqual(other.put(self.url(run2), json={"value": -1}).status_code, 200)
        with Session(self.eng) as db:
            q = db.get(Question, self.qid)
            review = db.get(QuestionVoteReview, self.qid)
            self.assertEqual(q.vote_score, -2)
            self.assertEqual(review.score_at_trigger, -2)
            self.assertEqual(review.status, "pending")
            self.assertEqual(db.scalar(select(func.count(QuestionVote.question_id))), 2)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)
        self.assertEqual(self.review_task.call_count, 1)

    def test_rejects_unshown_answered_crossuser_and_invalid_votes(self):
        other = TestClient(app)
        self.addCleanup(other.close)
        bob = self.register(other, "VotingBob")
        self.assertEqual(other.put(self.url(self.run1), json={"value": 1}).status_code, 404)
        self.assertEqual(self.client.put(self.url(self.run1), json={"value": 9}).status_code, 422)
        self.assertEqual(self.client.put(self.url(self.run1), json={"value": True}).status_code, 422)
        with Session(self.eng) as db:
            item = db.scalar(select(QuestionAttempt).where(
                QuestionAttempt.run_id == self.run1))
            from datetime import datetime, timezone
            item.answered_at = datetime.now(timezone.utc)
            db.commit()
        self.assertEqual(self.client.put(self.url(self.run1), json={"value": 1}).status_code, 409)
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.qid).vote_score, 0)

    def test_real_worker_upholds_only_after_independent_models(self):
        self.client.put(self.url(self.run1), json={"value": -1})
        other = TestClient(app)
        self.addCleanup(other.close)
        bob = self.register(other, "VotingBob")
        run2 = self.add_attempt(bob)
        other.put(self.url(run2), json={"value": -1})

        def completion(messages, role, **kwargs):
            if role == "CADDY":
                return json.dumps({
                    "summary_en": "Assess whether the key is defensible.",
                    "claims_to_check": ["Key is accurate"],
                    "identified_issues": ["incorrect_key"],
                    "needs_external_fact_check": False,
                })
            return json.dumps({
                "decision": "upheld",
                "explanation_en": "A verified defect was found.",
                "explanation_player_language": "Вопрос содержит ошибку.",
                "rationale_facts": ["The stored answer is wrong."],
            })
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=completion
        ) as model, patch(
            "persistence.vote_worker._config", return_value=("gpt-6.1-sol", "low", 800, 40)
        ):
            self.assertEqual(process_vote_review(self.qid), "upheld")
            self.assertEqual(process_vote_review(self.qid), "upheld")
            self.assertEqual(model.call_count, 2)
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.qid).status, "rejected")
            moderation = db.get(QuestionModeration, self.qid)
            self.assertTrue(moderation.permanently_blocked)
            self.assertEqual(moderation.block_reason, "community_judge")
            self.assertEqual(db.scalar(select(func.count(QuestionReview.id))), 1)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)

    def test_rejected_review_does_not_block_question(self):
        self.client.put(self.url(self.run1), json={"value": -1})
        other = TestClient(app)
        self.addCleanup(other.close)
        bob = self.register(other, "VotingBob")
        other.put(self.url(self.add_attempt(bob)), json={"value": -1})
        caddy = json.dumps({"summary_en": "Subjective feedback only",
                            "claims_to_check": [], "identified_issues": [],
                            "needs_external_fact_check": False})
        judge = json.dumps({"decision": "rejected",
                            "explanation_en": "No objective defect.",
                            "explanation_player_language": "Вопрос корректен.",
                            "rationale_facts": []})
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=[caddy, judge]
        ), patch("persistence.vote_worker._config",
                 return_value=("gpt-6.1-sol", "low", 800, 40)):
            self.assertEqual(process_vote_review(self.qid), "rejected")
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.qid).status, "approved")
            self.assertEqual(db.get(QuestionVoteReview, self.qid).status, "rejected")
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)

    def test_model_failure_is_retried_without_removing_good_question(self):
        self.client.put(self.url(self.run1), json={"value": -1})
        with TestClient(app) as other:
            bob = self.register(other, "VotingBob")
            other.put(self.url(self.add_attempt(bob)), json={"value": -1})
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=RuntimeError("offline")
        ) as model:
            self.assertEqual(process_vote_review(self.qid), "retry_pending")
            self.assertEqual(process_vote_review(self.qid), "not_due")
            self.assertEqual(model.call_count, 1)
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.qid).status, "approved")
            review = db.get(QuestionVoteReview, self.qid)
            self.assertEqual(review.status, "retry_pending")
            self.assertEqual(review.attempts, 1)
            self.assertIsNotNone(review.next_retry_at)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)

    def test_same_generator_and_judge_never_adjudicate(self):
        self.client.put(self.url(self.run1), json={"value": -1})
        with TestClient(app) as other:
            bob = self.register(other, "VotingBob")
            other.put(self.url(self.add_attempt(bob)), json={"value": -1})
        with Session(self.eng) as db:
            db.get(Question, self.qid).generator_model = "gpt-6.1-sol"
            db.commit()
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion"
        ) as model:
            self.assertEqual(process_vote_review(self.qid), "model_conflict")
            model.assert_not_called()
        with Session(self.eng) as db:
            self.assertEqual(db.get(Question, self.qid).status, "approved")
            self.assertEqual(db.get(QuestionVoteReview, self.qid).status,
                             "retry_pending")



if __name__ == "__main__":
    unittest.main()
