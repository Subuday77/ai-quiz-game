"""Votes are provisional until answer; durable referrals and dual scores."""
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.gameplay import GameConflict, record_answer
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, QuestionModeration,
    QuestionReview, QuestionVote, QuestionVoteReview, Topic, User,
    WalletTransaction,
)
from persistence.question_source_policy import (
    admission_probability, community_vote_threshold, first_acquittal_score,
    max_referrals, question_source_order, rating_center, rating_scale,
    second_acquittal_score,
)
from persistence.vote_worker import process_vote_review


class VotePolicyTests(unittest.TestCase):
    def test_spec_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(question_source_order(), "generate_first")
            self.assertEqual(community_vote_threshold(), -15)
            self.assertEqual(first_acquittal_score(), -5)
            self.assertEqual(second_acquittal_score(), -10)
            self.assertEqual(max_referrals(), 3)
            self.assertEqual(rating_center(), 0)
            self.assertEqual(rating_scale(), 10)
        with patch.dict(os.environ, {
            "QUIZ_DB_FIRST_GENERATION": "true",
            "QUIZ_QUESTION_RATING_REVIEW_THRESHOLD": "-12",
            "QUIZ_DB_FIRST_RATING_CENTER": "5",
            "QUIZ_DB_FIRST_RATING_SCALE": "3",
        }):
            self.assertEqual(question_source_order(), "pool_first")
            self.assertEqual(community_vote_threshold(), -12)
            self.assertEqual(rating_center(), 5)
            self.assertEqual(rating_scale(), 3)
        for text in ("", "truee", "1", "pool_first"):
            with self.subTest(text=text), patch.dict(
                    os.environ, {"QUIZ_DB_FIRST_GENERATION": text}):
                with self.assertRaises(ValueError):
                    question_source_order()
        for bad in ("0", "3", "abc", "-100001"):
            with self.subTest(bad=bad), patch.dict(
                    os.environ, {"QUIZ_QUESTION_RATING_REVIEW_THRESHOLD": bad}):
                with self.assertRaises(ValueError):
                    community_vote_threshold()
        for bad in ("0", "-2", "nan", "inf", "hello"):
            with self.subTest(bad=bad), patch.dict(
                    os.environ, {"QUIZ_DB_FIRST_RATING_SCALE": bad}):
                with self.assertRaises(ValueError):
                    rating_scale()

    def test_logistic_admission_is_not_a_selection_probability(self):
        import random
        expected = { -10: .26894, -5: .37754, 0: .5, 5: .62246, 10: .73106 }
        for rating, target in expected.items():
            self.assertAlmostEqual(
                admission_probability(rating, center=0, scale=10),
                target, delta=.0001,
            )
        self.assertLess(admission_probability(-10, center=5, scale=10),
                        admission_probability(-10, center=0, scale=10))
        self.assertGreater(admission_probability(-10, center=-5, scale=10),
                           admission_probability(-10, center=0, scale=10))
        rng = random.Random(42)
        for rating in (-10, -5, 0, 5, 10):
            threshold = admission_probability(rating, center=0, scale=10)
            successes = sum(rng.random() < threshold for _ in range(20000))
            self.assertAlmostEqual(successes / 20000, threshold, delta=.015)


class AnswerCommittedVotesTests(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.eng, "connect")
        def enable_foreign_keys(conn, _):
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
            "QUIZ_QUESTION_RATING_REVIEW_THRESHOLD": "-15",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "gpt-6.1-sol",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        registered = self.client.post("/api/auth/register", json={
            "username": "VotingAlice",
            "password": "offline-test-password-2026!",
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        self.alice = UUID(registered.json()["id"])
        with Session(self.eng, expire_on_commit=False) as db:
            topic = Topic(slug="vote-fixture-science", labels={"en": "Science"})
            db.add(topic)
            db.flush()
            question = Question(
                topic_id=topic.id, language="English", requested_difficulty=0,
                text="Which human organ produces adrenaline?",
                choices={"A": "Thyroid", "B": "Adrenal gland",
                         "C": "Liver", "D": "Pancreas"},
                correct_key="B", fingerprint=uuid4().hex + uuid4().hex,
                status="approved", generator_provider="openai",
                generator_model="gpt-5.4", prompt_version="qa",
                schema_version="question_tagged_v3",
            )
            db.add(question)
            db.commit()
            self.qid = question.id
        self.run1 = self.add_attempt(self.alice)

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

    def answer_url(self, run):
        return f"/api/v2/games/{run}/answer"

    def answer(self, run, *, vote=None, answer="A"):
        payload = {"question_id": str(self.qid), "answer": answer}
        if vote is not None:
            payload["vote"] = vote
        return self.client.post(self.answer_url(run), json=payload)

    def direct_player(self, index):
        with Session(self.eng, expire_on_commit=False) as db:
            account = User(
                username=f"Vote{index:03d}",
                username_key=f"vote{index:03d}",
            )
            db.add(account)
            db.flush()
            uid = account.id
            db.commit()
        return uid

    def direct_answer(self, uid, *, value=-1, run_id=None):
        run_id = run_id or self.add_attempt(uid)
        with Session(self.eng) as db:
            return record_answer(
                db, run_id=run_id, user_id=uid,
                question_id=self.qid, selected="A", vote=value,
            )

    def score(self):
        with Session(self.eng) as db:
            q = db.get(Question, self.qid)
            return q.vote_score, q.working_vote_score, q.community_referrals, q.status

    def test_unanswered_has_no_ballot_or_leaked_counts(self):
        get = self.client.get(f"/api/v2/games/{self.run1}/question")
        self.assertEqual(get.status_code, 200, get.text)
        for field in ("correct_answer", "vote_score", "working_vote_score",
                      "my_vote", "community_referrals"):
            self.assertNotIn(field, get.json())
        self.assertEqual(self.score(), (0, 0, 0, "approved"))
        self.assertEqual(self.client.put(
            f"/api/v2/games/{self.run1}/questions/{self.qid}/vote",
            json={"value": -1}).status_code, 404)
        self.assertEqual(self.score(), (0, 0, 0, "approved"))

    def test_answer_commits_once_and_repeat_is_nonmutating(self):
        first = self.answer(self.run1, vote=-1)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.score(), (-1, -1, 0, "approved"))
        with Session(self.eng) as db:
            ballot = db.get(QuestionVote, (self.qid, self.alice))
            self.assertEqual(ballot.run_id, self.run1)
            self.assertEqual(ballot.value, -1)
        retry = self.answer(self.run1, vote=1)
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(self.score(), (-1, -1, 0, "approved"))
        second = self.add_attempt(self.alice)
        with Session(self.eng) as db:
            record_answer(
                db, run_id=second, user_id=self.alice,
                question_id=self.qid, selected="A", vote=1,
            )
        self.assertEqual(self.score(), (-1, -1, 0, "approved"))
        with Session(self.eng) as db:
            self.assertEqual(db.scalar(select(func.count(QuestionVote.question_id))), 1)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)

    def test_none_vote_and_invalid_inputs_do_not_change_db(self):
        for bad in (True, 2, -2, "1"):
            with self.subTest(bad=bad):
                response = self.answer(self.run1, vote=bad)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.score(), (0, 0, 0, "approved"))
        self.assertEqual(self.answer(self.run1).status_code, 200)
        self.assertEqual(self.score(), (0, 0, 0, "approved"))
        with Session(self.eng) as db:
            self.assertEqual(db.scalar(select(func.count(QuestionVote.question_id))), 0)

    def test_cross_user_wrong_run_is_rejected(self):
        with TestClient(app) as other:
            result = other.post("/api/auth/register", json={
                "username": "VotingBob",
                "password": "offline-test-password-2026!",
            })
            self.assertEqual(result.status_code, 201, result.text)
            res = other.post(self.answer_url(self.run1), json={
                "question_id": str(self.qid), "answer": "A", "vote": -1,
            })
            self.assertEqual(res.status_code, 404)
        self.assertEqual(self.score(), (0, 0, 0, "approved"))

    def test_referral_quarantine_and_two_acquittals_then_auto_ban(self):
        # First 15 permanent dislikes hit the threshold exactly.
        for index in range(1, 16):
            self.direct_answer(self.direct_player(index))
        self.assertEqual(self.score(), (-15, -15, 1, "quarantined"))
        with Session(self.eng) as db:
            self.assertEqual(db.get(QuestionVoteReview, (self.qid, 1)).status, "pending")

        caddy = json.dumps({
            "summary_en": "Subjective negative rating, check facts independently.",
            "claims_to_check": [], "identified_issues": [],
            "needs_external_fact_check": False,
        })
        acquit = json.dumps({
            "decision": "rejected", "explanation_en": "Valid question.",
            "explanation_player_language": "Вопрос корректен.",
            "rationale_facts": [],
        })
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=[caddy, acquit]
        ), patch("persistence.vote_worker._config",
                 return_value=("gpt-6.1-sol", "low", 800, 40)):
            self.assertEqual(process_vote_review(self.qid), "rejected")
        self.assertEqual(self.score(), (-15, -5, 1, "approved"))

        # Ten additional players lower working score from -5 to -15.
        for index in range(16, 26):
            self.direct_answer(self.direct_player(index))
        self.assertEqual(self.score(), (-25, -15, 2, "quarantined"))
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=[caddy, acquit]
        ), patch("persistence.vote_worker._config",
                 return_value=("gpt-6.1-sol", "low", 800, 40)):
            self.assertEqual(process_vote_review(self.qid), "rejected")
        self.assertEqual(self.score(), (-25, -10, 2, "approved"))

        # Third crossing: permanent ban *without* another model request.
        for index in range(26, 31):
            self.direct_answer(self.direct_player(index))
        self.assertEqual(self.score(), (-30, -15, 3, "rejected"))
        with Session(self.eng) as db:
            review = db.get(QuestionVoteReview, (self.qid, 3))
            self.assertEqual(review.status, "auto_banned")
            self.assertEqual(db.get(QuestionModeration, self.qid).block_reason,
                             "community_third_referral")
            self.assertEqual(db.scalar(select(func.count(QuestionVote.question_id))), 30)
            self.assertEqual(db.scalar(select(func.count(QuestionVoteReview.question_id))), 3)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion"
        ) as no_ai:
            self.assertEqual(process_vote_review(self.qid), "auto_banned")
            no_ai.assert_not_called()

    def test_judge_upheld_permanently_bans_at_first_referral(self):
        for index in range(1, 16):
            self.direct_answer(self.direct_player(index))
        caddy = json.dumps({
            "summary_en": "Question fact wrong.", "claims_to_check": ["Answer key"],
            "identified_issues": ["incorrect_key"], "needs_external_fact_check": False,
        })
        judge = json.dumps({
            "decision": "upheld", "explanation_en": "Incorrect key.",
            "explanation_player_language": "Неверный ответ.",
            "rationale_facts": ["Incorrect key"],
        })
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=[caddy, judge]
        ) as model, patch("persistence.vote_worker._config",
                 return_value=("gpt-6.1-sol", "low", 800, 40)):
            self.assertEqual(process_vote_review(self.qid), "upheld")
            self.assertEqual(process_vote_review(self.qid), "upheld")
            self.assertEqual(model.call_count, 2)
        self.assertEqual(self.score(), (-15, -15, 1, "rejected"))
        with Session(self.eng) as db:
            self.assertEqual(db.get(QuestionModeration, self.qid).block_reason,
                             "community_judge")
            self.assertEqual(db.scalar(select(func.count(QuestionReview.id))), 1)
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 0)

    def test_model_conflict_never_self_judges(self):
        for index in range(1, 16):
            self.direct_answer(self.direct_player(index))
        with Session(self.eng) as db:
            db.get(Question, self.qid).generator_model = "gpt-6.1-sol"
            db.commit()
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion"
        ) as model:
            self.assertEqual(process_vote_review(self.qid), "model_conflict")
            model.assert_not_called()
        self.assertEqual(self.score(), (-15, -15, 1, "quarantined"))

    def test_provider_failure_schedules_retry_and_keeps_quarantine(self):
        for index in range(1, 16):
            self.direct_answer(self.direct_player(index))
        with patch("persistence.vote_worker.engine", return_value=self.eng), patch(
            "persistence.vote_worker._completion", side_effect=RuntimeError("offline")
        ) as model:
            self.assertEqual(process_vote_review(self.qid), "retry_pending")
            self.assertEqual(process_vote_review(self.qid), "not_due")
            self.assertEqual(model.call_count, 1)
        self.assertEqual(self.score(), (-15, -15, 1, "quarantined"))
        with Session(self.eng) as db:
            review = db.get(QuestionVoteReview, (self.qid, 1))
            self.assertEqual(review.attempts, 1)
            self.assertIsNotNone(review.next_retry_at)

if __name__ == "__main__":
    unittest.main()
