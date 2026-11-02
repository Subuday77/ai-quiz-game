"""Offline global-question-pool and strict emergency reserve regressions."""
import os
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.game_routes import _category_for_round
from persistence.gameplay import create_game, offer_question
from persistence.models import (
    Base, Question, QuestionAttempt, QuestionReview,
)
from persistence.question_pool import (
    enabled, offer_approved_pool_question, scan_limit,
)
from persistence.seed_topics import seed_topics
from tagging.review import is_strict_too_easy_reserve
from test_tagged_dedup import candidate


def review_report():
    return {
        "checker_type": "ai_tagged_critic",
        "provider": "openai",
        "model": "test",
        "contract_version": "tagged_review_v5",
        "decision": "accept",
        "issues": [],
        "details": {"assessment": {"total": 0}, "verdict": {"facts": "pass"}},
        "duration_ms": 1,
    }


def too_easy_report():
    return {
        **review_report(),
        "decision": "reject",
        "issues": ["difficulty_mismatch"],
        "details": {
            "verdict": {
                "difficulty": "too_easy",
                "issue_codes": ["difficulty_mismatch"],
                "facts": "pass", "options": "pass", "language": "pass",
                "leakage": "pass", "tags_valid": "pass", "fact_valid": "pass",
                "semantic_duplicate": False, "premise": "valid",
            },
            "assessment": {
                "critical": "none",
                "raw_penalties": {
                    "distractors": 0, "knowledge": 0, "suitability": 0, "wording": 0,
                },
            },
            "option_diagnostics": {
                "outcome": "single_correct",
                "supported_choices": ["B"],
                "uncertain_choices": [],
            },
            "safeguard_codes": [],
        },
    }


class StrictReserveTests(unittest.TestCase):
    def test_only_too_easy_and_no_other_penalty_is_eligible(self):
        q = candidate()
        q._review_report = too_easy_report()
        self.assertTrue(is_strict_too_easy_reserve(q))
        for key in ("distractors", "knowledge", "suitability", "wording"):
            with self.subTest(penalty=key):
                bad = q.model_copy(deep=True)
                bad._review_report["details"]["assessment"]["raw_penalties"][key] = 1
                self.assertFalse(is_strict_too_easy_reserve(bad))
        for path, value in [
            (("verdict", "facts"), "fail"),
            (("verdict", "options"), "uncertain"),
            (("verdict", "tags_valid"), "fail"),
            (("verdict", "semantic_duplicate"), True),
            (("assessment", "critical"), "regenerate"),
            (("option_diagnostics", "outcome"), "multiple_correct"),
        ]:
            with self.subTest(path=path):
                bad = q.model_copy(deep=True)
                bad._review_report["details"][path[0]][path[1]] = value
                self.assertFalse(is_strict_too_easy_reserve(bad))
        bad = q.model_copy(deep=True)
        bad._review_report["details"]["verdict"]["issue_codes"].append("bad_distractors")
        self.assertFalse(is_strict_too_easy_reserve(bad))
        bad = q.model_copy(deep=True)
        bad._review_report["decision"] = "accept"
        self.assertFalse(is_strict_too_easy_reserve(bad))


class PoolConfigTests(unittest.TestCase):
    def test_flag_and_scan_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(enabled("QUIZ_QUESTION_POOL_FALLBACK_ENABLED"))
            self.assertEqual(scan_limit(), 100)
        for v in ("0", "maybe", ""):
            with patch.dict(os.environ, {"QUIZ_QUESTION_POOL_FALLBACK_ENABLED": v}):
                with self.assertRaises(ValueError):
                    enabled("QUIZ_QUESTION_POOL_FALLBACK_ENABLED")
        for v in ("0", "1001", "nan", "2.5"):
            with patch.dict(os.environ, {"QUIZ_QUESTION_POOL_SCAN_LIMIT": v}):
                with self.assertRaises(ValueError):
                    scan_limit()


class GlobalPoolTests(unittest.TestCase):
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
        with Session(self.engine) as db:
            seed_topics(db)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.enterContext(patch.dict(os.environ, {
            "AUTH_COOKIE_SECURE": "false",
            "AI_TAG_DEDUP_ENABLED": "true",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "true",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "true",
            "QUIZ_QUESTION_POOL_SCAN_LIMIT": "100",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.alice = self._register(self.client, "Alice")
        response = self.client.post("/api/v2/games", json={"language": "English"})
        self.assertEqual(response.status_code, 201, response.text)
        self.run_id = UUID(response.json()["game_id"])
        self.category = _category_for_round(self.run_id, 1)
        with TestClient(app) as other:
            self.bob = self._register(other, "Bob")

    def _register(self, client, name):
        response = client.post("/api/auth/register", json={
            "username": name, "password": "offline-test-password-2026!",
        })
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["id"])

    def seed_other_user_question(self, *, accepted=True):
        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            bob_run = create_game(db, user_id=self.bob, language="English")
            q = candidate()
            if accepted:
                q._review_report = review_report()
            attempt, saved = offer_question(
                db, run_id=bob_run.id, user_id=self.bob, question=q,
                category=self.category, provider="test", model="stub",
                dedup_enforce=True,
            )
            return saved.id

    def request(self):
        return self.client.get(f"/api/v2/games/{self.run_id}/question")

    def test_emergency_reuses_other_users_question_without_ai_or_copy(self):
        existing_id = self.seed_other_user_question()
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )) as gen:
            response = self.request()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["question_id"], str(existing_id))
            self.assertNotIn("correct_answer", response.json())
            self.assertNotIn("fact", response.json())
            self.assertEqual(self.request().json(), response.json())
            gen.assert_called_once()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionReview.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_pool_does_not_reissue_to_same_user(self):
        existing_id = self.seed_other_user_question()
        with Session(self.engine) as db:
            attempt, saved = offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            )
            self.assertEqual(saved.id, existing_id)
            self.assertIsNotNone(attempt)
        # A fresh Alice game at the same difficulty/category must not get
        # the already SHOWN fact, even though Alice has not answered it.
        with Session(self.engine) as db:
            second_run = create_game(db, user_id=self.alice, language="English")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=second_run.id, user_id=self.alice,
                category=self.category,
            ))
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_semantically_same_fact_in_other_language_is_not_reissued(self):
        # Exact-question exclusion alone would miss this: Alice saw the fact
        # in German, Bob's approved reusable question is in English.
        self.seed_other_user_question()
        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            german_run = create_game(db, user_id=self.alice, language="German")
            offer_question(
                db, run_id=german_run.id, user_id=self.alice,
                question=candidate(language="German"),
                category=self.category, provider="test", model="stub",
                dedup_enforce=True,
            )
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            ))
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_pool_requires_exact_language_category_and_difficulty(self):
        self.seed_other_user_question()
        with Session(self.engine) as db:
            german_run = create_game(db, user_id=self.alice, language="German")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=german_run.id, user_id=self.alice,
                category=self.category,
            ))
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category="A category with no matching question",
            ))
            from persistence.models import GameRun
            alice_run = db.get(GameRun, self.run_id)
            alice_run.correct_main_answers = 1
            db.commit()
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            ))

    def test_no_review_no_pool(self):
        self.seed_other_user_question(accepted=False)
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )):
            response = self.request()
        self.assertEqual(response.status_code, 503)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)

    def test_strict_too_easy_reserve_is_last_resort_and_not_in_future_pool(self):
        q = candidate()
        q._review_report = too_easy_report()

        def exhausted(*args, **kwargs):
            kwargs["too_easy_reserve"].append(q.model_copy(deep=True))
            raise HTTPException(status_code=502, detail="AI could not generate a valid question")

        with patch("api.generate_valid_question", side_effect=exhausted):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as db:
            review = db.scalar(select(QuestionReview))
            self.assertEqual(review.decision, "fallback_too_easy")
            self.assertEqual(review.details["fallback"]["original_decision"], "reject")
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            second = create_game(db, user_id=self.bob, language="English")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=second.id, user_id=self.bob, category=self.category,
            ))

    def test_an_additional_penalty_excludes_reserve(self):
        q = candidate()
        q._review_report = too_easy_report()
        q._review_report["details"]["assessment"]["raw_penalties"]["distractors"] = 1

        def exhausted(*args, **kwargs):
            kwargs["too_easy_reserve"].append(q)
            raise HTTPException(status_code=502, detail="AI could not generate a valid question")

        with patch("api.generate_valid_question", side_effect=exhausted):
            response = self.request()
        self.assertEqual(response.status_code, 502)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 0)

    def test_pool_has_priority_over_too_easy_reserve(self):
        existing_id = self.seed_other_user_question()
        q = candidate(relation="prize", concept="prize", answer="photoelectric-effect")
        q._review_report = too_easy_report()

        def exhausted(*args, **kwargs):
            kwargs["too_easy_reserve"].append(q)
            raise HTTPException(status_code=503, detail="AI provider unavailable")

        with patch("api.generate_valid_question", side_effect=exhausted):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(existing_id))
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)


if __name__ == "__main__":
    unittest.main()
