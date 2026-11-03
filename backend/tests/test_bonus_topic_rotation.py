"""Offline PostgreSQL-contract tests for unlimited Q16 topic recovery."""
import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from consts import QUESTION_CATEGORIES
from persistence.database import get_db
from persistence.game_routes import _category_for_round
from persistence.models import Base, GameRun, QuestionAttempt
from persistence.seed_topics import seed_topics
from sqlalchemy import func, select


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
            db.commit()

    def failed_generation(self):
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="Test generator unavailable"
        )) as gen:
            failed = self.client.get(self.base + "/question")
        self.assertEqual(failed.status_code, 503, failed.text)
        gen.assert_called_once()

    def test_main_cannot_change_topic_even_after_failure(self):
        self.assertEqual(self.client.post(self.base + "/bonus/change-topic").status_code, 409)
        self.failed_generation()
        self.assertEqual(self.client.post(self.base + "/bonus/change-topic").status_code, 409)
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.game_id))
            self.assertFalse(run.bonus_topic_change_unlocked)

    def test_bonus_can_change_every_category_and_cycle_without_resetting_progress(self):
        self.set_bonus()
        self.assertEqual(self.client.post(self.base + "/bonus/change-topic").status_code, 409)
        self.failed_generation()
        with Session(self.engine) as db:
            self.assertTrue(db.get(GameRun, UUID(self.game_id)).bonus_topic_change_unlocked)
        originals = []
        for i in range(1, len(QUESTION_CATEGORIES) + 1):
            response = self.client.post(self.base + "/bonus/change-topic")
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["topic_index"], i % len(QUESTION_CATEGORIES))
            self.assertEqual(data["cycle_reset"], i == len(QUESTION_CATEGORIES))
            self.assertEqual(data["category"], _category_for_round(
                UUID(self.game_id), 16, i
            ))
            originals.append(data["category"])
        self.assertEqual(len(set(originals)), len(QUESTION_CATEGORIES))
        self.assertEqual(originals[-1], _category_for_round(UUID(self.game_id), 16))
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.game_id))
            self.assertEqual(run.status, "bonus")
            self.assertEqual(run.correct_main_answers, 15)
            self.assertEqual(run.bonus_topic_index, 0)
            self.assertTrue(run.bonus_topic_change_unlocked)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 0)

    def test_rotation_survives_reload_and_success_blocks_further_changes(self):
        self.set_bonus()
        self.failed_generation()
        changed = self.client.post(self.base + "/bonus/change-topic")
        self.assertEqual(changed.status_code, 200)
        with TestClient(app) as reopened:
            reopened.cookies.update(self.client.cookies)
            self.assertEqual(reopened.get(self.base).json()["phase"], "bonus")
            generated = {
                "difficulty": 15,
                "question": "What is an unusual but verifiable historical fact?",
                "answers": {"A": "Value A", "B": "Value B",
                            "C": "Value C", "D": "Value D"},
                "correct_answer": "B",
            }
            with patch("api.generate_valid_question", return_value=generated) as gen:
                response = reopened.get(self.base + "/question")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["number"], 16)
            self.assertTrue(response.json()["bonus"])
            self.assertEqual(gen.call_args.args[2], changed.json()["category"])
            self.assertEqual(reopened.post(
                self.base + "/bonus/change-topic").status_code, 409)
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.game_id))
            self.assertFalse(run.bonus_topic_change_unlocked)
            self.assertEqual(run.correct_main_answers, 15)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)

    def test_bonus_change_respects_origin_and_authentication(self):
        self.set_bonus()
        self.failed_generation()
        self.assertEqual(self.client.post(
            self.base + "/bonus/change-topic",
            headers={"Origin": "https://not-our-site.invalid"}
        ).status_code, 403)
        with TestClient(app) as outsider:
            self.assertEqual(outsider.post(
                self.base + "/bonus/change-topic").status_code, 401)


if __name__ == "__main__":
    unittest.main()
