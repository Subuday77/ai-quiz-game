"""Offline regression tests for system-owned automatic Q16 topic rotation.

All generated questions are mocked; no network, paid provider or PostgreSQL
connection is required. The SQLite transaction contract mirrors GameRun.
"""
import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from consts import QUESTION_CATEGORIES
from persistence.database import get_db
from persistence.game_routes import _category_for_round
from persistence.models import Base, GameRun, QuestionAttempt
from persistence.seed_topics import seed_topics
from question_schema import QuizQuestion


class BonusTopicRotationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def fk_on(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
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
            "AI_TAG_DEDUP_ENABLED": "false",
            "AI_QUALITY_MODE": "off",
            "AI_LOCAL_LEAK_MODE": "off",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "false",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "false",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        register = self.client.post("/api/auth/register", json={
            "username": "BonusTester", "password": "offline-bonus-password-2026!",
        })
        self.assertEqual(register.status_code, 201, register.text)
        game = self.client.post("/api/v2/games", json={"language": "English"})
        self.assertEqual(game.status_code, 201, game.text)
        self.game_id = game.json()["game_id"]
        self.base = f"/api/v2/games/{self.game_id}"

    def set_bonus(self):
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.game_id))
            run.status = "bonus"
            run.correct_main_answers = 15
            run.bonus_wager_accepted = True  # Q16 generation is authorized in this fixture.
            db.commit()

    def get_state(self):
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.game_id))
            return (run.status, run.correct_main_answers,
                    run.bonus_topic_index, run.bonus_topic_change_unlocked,
                    db.scalar(select(func.count(QuestionAttempt.id))))

    def failed_generation(self, status=503, client=None):
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=status, detail="Test generator unavailable",
        )) as gen:
            failed = (client or self.client).get(self.base + "/question")
        self.assertEqual(failed.status_code, status, failed.text)
        gen.assert_called_once()
        return gen.call_args.args[2]

    def test_main_failure_does_not_rotate_any_topic(self):
        old = self.get_state()
        original = _category_for_round(UUID(self.game_id), 1)
        self.assertEqual(self.failed_generation(), original)
        self.assertEqual(self.get_state(), old)
        self.assertEqual(self.client.post(self.base + "/bonus/change-topic").status_code, 404)

    def test_every_failed_bonus_generation_auto_rotates_without_player_action(self):
        self.set_bonus()
        original = _category_for_round(UUID(self.game_id), 16)
        initial = self.get_state()
        self.assertEqual(initial[2], 0)
        visited = []
        for i in range(1, len(QUESTION_CATEGORIES) + 2):
            generated_for = self.failed_generation(status=502)
            visited.append(generated_for)
            self.assertEqual(generated_for, _category_for_round(
                UUID(self.game_id), 16, i - 1
            ))
            state = self.get_state()
            self.assertEqual(state[0:2], ("bonus", 15))
            self.assertEqual(state[2], i % len(QUESTION_CATEGORIES))
            self.assertTrue(state[3])
            self.assertEqual(state[4], 0)
        self.assertEqual(len(set(visited[:-1])), len(QUESTION_CATEGORIES))
        self.assertEqual(visited[-1], original)
        # The user has NO public API to pick a topic; the server may later
        # expose a separate explicit *star perk*.
        self.assertEqual(self.client.post(self.base + "/bonus/change-topic").status_code, 404)

    def test_auto_rotation_survives_restart_and_success_finishes_recovery(self):
        self.set_bonus()
        first = self.failed_generation()
        self.assertEqual(first, _category_for_round(UUID(self.game_id), 16))
        with TestClient(app) as reopened:
            reopened.cookies.update(self.client.cookies)
            self.assertEqual(reopened.get(self.base).json()["phase"], "bonus")
            generated = QuizQuestion.model_validate({
                "difficulty": 15,
                "question": "What is an unusual but verifiable historical fact?",
                "answers": {"A": "Value A", "B": "Value B",
                            "C": "Value C", "D": "Value D"},
                "correct_answer": "B",
            })
            with patch("api.generate_valid_question", return_value=generated) as gen:
                response = reopened.get(self.base + "/question")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["number"], 16)
            self.assertTrue(response.json()["bonus"])
            self.assertEqual(gen.call_args.args[2], _category_for_round(
                UUID(self.game_id), 16, 1
            ))
            # Subsequent GET must reuse the same issued question.
            self.assertEqual(reopened.get(
                self.base + "/question").json(), response.json())
            self.assertEqual(reopened.post(
                self.base + "/bonus/change-topic").status_code, 404)
        self.assertEqual(self.get_state(), ("bonus", 15, 1, False, 1))

    def test_tagged_bonus_empty_pool_does_not_publish_too_easy_reserve(self):
        self.set_bonus()
        with patch.dict(os.environ, {
            "AI_TAG_DEDUP_ENABLED": "true",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "true",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "true",
        }), patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=502, detail="No acceptable bonus candidate",
        )) as gen:
            result = self.client.get(self.base + "/question")
        self.assertEqual(result.status_code, 502, result.text)
        gen.assert_called_once()
        self.assertEqual(self.get_state(), ("bonus", 15, 1, True, 0))

    def test_concurrent_failures_cannot_rotate_the_same_topic_twice(self):
        from persistence.game_routes import _advance_bonus_topic_after_failure

        self.set_bonus()
        self.failed_generation(status=502)
        with Session(self.engine) as db:
            _advance_bonus_topic_after_failure(
                db, run_id=UUID(self.game_id),
                user_id=self._user_id(db), expected_topic_index=0,
                failure_status=502,
            )
        self.assertEqual(self.get_state(), ("bonus", 15, 1, True, 0))

    def _user_id(self, db):
        return db.get(GameRun, UUID(self.game_id)).user_id

    def test_only_real_generator_failures_rotate_not_client_requests(self):
        self.set_bonus()
        with TestClient(app) as outsider:
            self.assertEqual(outsider.get(self.base + "/question").status_code, 401)
        self.assertEqual(self.client.post(
            self.base + "/bonus/change-topic",
            headers={"Origin": "https://untrusted.example"},
        ).status_code, 404)
        self.assertEqual(self.get_state(), ("bonus", 15, 0, False, 0))


if __name__ == "__main__":
    unittest.main()
