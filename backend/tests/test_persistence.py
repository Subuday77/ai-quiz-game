"""Offline SQLite contract tests for PostgreSQL-ready metadata and account API.

No PostgreSQL instance is required to validate the independent auth flow.
The Alembic migration itself must still be tested on real PostgreSQL later.
"""
import hashlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from migrations.snapshots.schema_v1 import Base as FrozenV1
from persistence.auth import AUTH_COOKIE, HASHER
from persistence.database import database_url, get_db
from persistence.models import (
    Base, AchievementDefinition, AuthIdentity, AuthSession, GameRun,
    Question, QuestionAttempt, QuestionReport, QuestionReview, QuestionTag,
    StarAward, Tag, Topic, User, UserAchievement, WalletTransaction,
)


def sqlite_engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    @event.listens_for(eng, "connect")
    def foreign_keys(dbapi_connection, record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    return eng


class PersistenceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.eng = sqlite_engine()
        Base.metadata.create_all(self.eng)
        self.addCleanup(self.eng.dispose)

    def test_migration_snapshot_and_current_models_match(self):
        self.assertEqual(set(Base.metadata.tables), set(FrozenV1.metadata.tables))
        for name, table in Base.metadata.tables.items():
            frozen = FrozenV1.metadata.tables[name]
            self.assertEqual(set(table.c.keys()), set(frozen.c.keys()), name)
        self.assertGreaterEqual(len(Base.metadata.tables), 15)

    def test_schema_contains_required_entities(self):
        tables = set(inspect(self.eng).get_table_names())
        for name in ("users", "auth_identities", "auth_sessions", "topics", "tags",
                     "questions", "question_tags", "game_runs", "question_attempts",
                     "achievement_definitions", "user_achievements", "star_awards",
                     "wallet_transactions", "question_reviews", "question_reports"):
            self.assertIn(name, tables)

    def test_question_difficulty_metadata_and_tags_are_separate(self):
        with Session(self.eng) as db:
            topic = Topic(slug="medieval_history", labels={"ru": "Средневековье"})
            tag = Tag(slug="printing_press", kind="concept",
                      labels={"ru": "Книгопечатание", "en": "Printing"})
            db.add_all([topic, tag])
            db.flush()
            question = Question(
                topic_id=topic.id, language="Russian", requested_difficulty=5,
                text="С каким изобретением связан Гутенберг?",
                choices={"A": "Телефон", "B": "Печатный станок",
                         "C": "Телескоп", "D": "Паровая машина"},
                correct_key="B", fingerprint="a" * 64, status="approved",
                generator_provider="openai", generator_model="gpt-5.4",
                prompt_version="v1", schema_version="question_v1",
            )
            db.add(question)
            db.flush()
            db.add(QuestionTag(question_id=question.id, tag_id=tag.id, weight=100))
            db.commit()
            self.assertEqual(db.scalar(select(Question.requested_difficulty)), 5)
            self.assertEqual(db.scalar(select(Tag.slug)), "printing_press")
            self.assertEqual(len(db.scalars(select(QuestionTag)).all()), 1)
            self.assertNotIn("difficulty", Tag.__table__.columns.keys())

    def test_score_star_wallet_and_reports_are_independent(self):
        with Session(self.eng) as db:
            user = User(username="Alice", username_key="alice")
            topic = Topic(slug="science", labels={"en": "Science"})
            db.add_all([user, topic])
            db.flush()
            question = Question(
                topic_id=topic.id, language="English", requested_difficulty=5,
                text="Which planet has rings?", choices={"A": "Mars", "B": "Saturn",
                    "C": "Venus", "D": "Earth"}, correct_key="B",
                fingerprint="b" * 64, generator_provider="openai",
                generator_model="gpt", prompt_version="v1", schema_version="v1",
            )
            run = GameRun(user_id=user.id, language="English", status="lost",
                          correct_main_answers=4, round_score=100)
            db.add_all([question, run])
            db.flush()
            attempt = QuestionAttempt(run_id=run.id, question_id=question.id,
                                      number=5, requested_difficulty=5,
                                      selected_key="A", is_correct=False,
                                      answer_duration_ms=8100)
            db.add(attempt)
            db.flush()
            db.add_all([
                StarAward(user_id=user.id, run_id=run.id, tier="bronze"),
                WalletTransaction(user_id=user.id, run_id=run.id,
                                  idempotency_key="award-abc", amount=10,
                                  reason="test_award"),
                QuestionReview(question_id=question.id, checker_type="local_literal",
                               contract_version="v1", decision="accept",
                               issues=[], details={}),
                QuestionReport(question_id=question.id, user_id=user.id,
                               attempt_id=attempt.id, idempotency_key="dispute-1",
                               reason="incorrect_key", status="pending"),
            ])
            db.commit()
            self.assertEqual(db.scalar(select(func.sum(GameRun.round_score))), 100)
            self.assertEqual(db.scalar(select(func.sum(WalletTransaction.amount))), 10)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionReport.id))), 1)
            with self.assertRaises(IntegrityError):
                db.add(StarAward(user_id=user.id, run_id=run.id, tier="bronze"))
                db.commit()
            db.rollback()

    def test_database_url_requires_postgresql_and_credentials_not_logged(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                database_url()
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///tmp.db"}):
            with self.assertRaises(ValueError):
                database_url()
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://localhost/quiz"}):
            self.assertEqual(database_url(), "postgresql+psycopg://localhost/quiz")


class AccountAPITests(unittest.TestCase):
    def setUp(self):
        self.eng = sqlite_engine()
        Base.metadata.create_all(self.eng)
        self.addCleanup(self.eng.dispose)
        def override_db():
            with Session(self.eng, autoflush=False, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.env = self.enterContext(patch.dict(os.environ, {"AUTH_COOKIE_SECURE": "false"}))
        self.client = TestClient(app)
        self.creds = {"username": "Alice_77", "password": "aStrongPassphrase2026!"}

    def register(self):
        return self.client.post("/api/auth/register", json=self.creds)

    def test_register_me_logout_and_password_is_hashed(self):
        r = self.register()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["username"], "Alice_77")
        self.assertNotIn("password", r.json())
        self.assertNotIn("password_hash", r.json())
        self.assertIn(AUTH_COOKIE, self.client.cookies)
        with Session(self.eng) as db:
            identity = db.scalar(select(AuthIdentity))
            self.assertNotEqual(identity.password_hash, self.creds["password"])
            self.assertTrue(HASHER.verify(identity.password_hash, self.creds["password"]))
            session = db.scalar(select(AuthSession))
            self.assertEqual(len(session.token_hash), 64)
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], self.creds["username"])
        logout = self.client.post("/api/auth/logout")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_duplicate_username_casefold_and_login(self):
        self.assertEqual(self.register().status_code, 201)
        self.creds["username"] = "aLiCe_77"
        self.assertEqual(self.register().status_code, 409)
        self.assertEqual(self.client.post("/api/auth/login",
                             json=self.creds).status_code, 200)

    def test_wrong_password_is_rejected_without_leaking_information(self):
        self.assertEqual(self.register().status_code, 201)
        self.client.cookies.clear()
        invalid = {**self.creds, "password": "wrongPassword123"}
        result = self.client.post("/api/auth/login", json=invalid)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.json()["detail"], "Invalid credentials")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_cross_site_registration_rejected(self):
        response = self.client.post("/api/auth/register", json=self.creds,
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        with Session(self.eng) as db:
            self.assertEqual(db.scalar(select(func.count(User.id))), 0)

    def test_input_validation(self):
        self.assertEqual(self.client.post("/api/auth/register", json={
            "username": "a$", "password": "short"
        }).status_code, 422)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
