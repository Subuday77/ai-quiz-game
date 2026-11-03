"""Opt-in tagged endpoint regression tests: history, HTTP status, and privacy."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import Base, Question, QuestionAttempt, QuestionFact
from persistence.seed_topics import seed_topics
from question_schema import PublicQuestion
from test_tagged_dedup import candidate


class TaggedEndpointTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def foreign_keys(dbapi_connection, record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
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
            "AI_QUALITY_MODE": "off",
            "AI_LOCAL_LEAK_MODE": "off",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        registration = self.client.post("/api/auth/register", json={
            "username": "Alice", "password": "offline-test-password-2026!",
        })
        self.assertEqual(registration.status_code, 201, registration.text)

    def new_game(self, language):
        response = self.client.post("/api/v2/games", json={"language": language})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["game_id"]

    def test_tagged_mode_persists_tags_without_leaking_them_to_browser(self):
        game_id = self.new_game("English")
        with patch("api.generate_valid_question", return_value=candidate()) as gen:
            result = self.client.get(f"/api/v2/games/{game_id}/question")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(set(result.json()), set(PublicQuestion.model_fields))
        self.assertNotIn("tags", result.json())
        self.assertNotIn("fact", result.json())
        self.assertTrue(gen.call_args.kwargs["tag_history"] == [])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Question)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(QuestionFact)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(QuestionAttempt)), 1)

    def test_duplicate_in_second_language_is_never_returned(self):
        first = self.new_game("English")
        with patch("api.generate_valid_question", return_value=candidate()):
            self.assertEqual(self.client.get(f"/api/v2/games/{first}/question").status_code, 200)
        second = self.new_game("German")
        with patch("api.generate_valid_question",
                   return_value=candidate(language="German")) as gen:
            response = self.client.get(f"/api/v2/games/{second}/question")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("question", response.json())
        self.assertEqual(len(gen.call_args.kwargs["tag_history"]), 1)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(QuestionAttempt)), 1)

    def test_duplicate_exhaustion_error_is_forwarded_without_question_or_progress_change(self):
        from fastapi import HTTPException
        game_id = self.new_game("German")
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=409, detail={"code": "duplicate_exhausted"}
        )) as gen:
            response = self.client.get(f"/api/v2/games/{game_id}/question")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json(), {"detail": {"code": "duplicate_exhausted"}})
        gen.assert_called_once()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Question)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(QuestionAttempt)), 0)

    def test_rejected_twice_then_critic_timeout_and_empty_pool_returns_503(self):
        from quality_errors import QualityRegenerationError, QualityReviewFailure

        game_id = self.new_game("English")
        with patch.dict(os.environ, {
            "AI_QUALITY_MODE": "enforce",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_CRITIC_MIN_REMAINING_SECONDS": "8",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "true",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "true",
        }), patch(
            "api.create_question", return_value=candidate().model_dump()
        ) as generator, patch(
            "tagging.review.check_tagged_quality",
            side_effect=[QualityRegenerationError(),
                         QualityRegenerationError(),
                         QualityReviewFailure("tag_review_unavailable", retryable=True)],
        ) as critic, patch(
            "persistence.question_pool.offer_approved_pool_question",
            return_value=None,
        ) as pool, patch("api.sleep") as sleep:
            response = self.client.get(f"/api/v2/games/{game_id}/question")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(generator.call_count, 3)
        self.assertEqual(critic.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        pool.assert_called_once()
        # No unreviewed question should leak into the active game or the DB.
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Question)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(QuestionAttempt)), 0)

    def test_history_without_backfill_blocks_tagged_generation(self):
        # Write the first question through the legacy route without tags.
        with patch.dict(os.environ, {"AI_TAG_DEDUP_ENABLED": "false"}):
            first = self.new_game("English")
            with patch("api.generate_valid_question", return_value=candidate().model_copy()):
                # Legacy generation receives QuizQuestion; the derived object
                # is acceptable to offer_question but is saved without metadata.
                response = self.client.get(f"/api/v2/games/{first}/question")
                self.assertEqual(response.status_code, 200, response.text)
        second = self.new_game("German")
        with patch("api.generate_valid_question") as gen:
            response = self.client.get(f"/api/v2/games/{second}/question")
            gen.assert_not_called()
        self.assertEqual(response.status_code, 503)
        self.assertIn("backfill", response.json()["detail"].lower())
