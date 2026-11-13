"""Offline regression tests for username suggestions and one-time About rewards."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import Base, User, WalletTransaction
from persistence.star_economy import star_balances


class SignupAndAboutBonusTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.dict("os.environ", {"AUTH_COOKIE_SECURE": "false"}))
        self.client = TestClient(app)

    def signup(self, username):
        return self.client.post("/api/auth/register", json={
            "username": username, "password": "safe-long-password-2026!",
        })

    def test_unavailable_name_suggests_real_unique_valid_names(self):
        self.assertEqual(self.signup("Vasya").status_code, 201)
        with Session(self.engine) as db:
            db.add(User(username="Vasya2026", username_key="vasya2026"))
            db.commit()
        self.assertEqual(self.signup("vasya").status_code, 409)
        response = self.client.get("/api/auth/username-suggestions", params={"username": "Vasya"})
        self.assertEqual(response.status_code, 200, response.text)
        suggestions = response.json()["suggestions"]
        self.assertEqual(len(suggestions), 3)
        self.assertEqual(len(set(v.lower() for v in suggestions)), 3)
        self.assertNotIn("Vasya2026", suggestions)
        self.assertTrue(all(v.startswith("Vasya") and len(v) <= 30 for v in suggestions))
        self.assertEqual(self.signup(suggestions[0]).status_code, 201)

    def test_long_usernames_and_invalid_name(self):
        name = "A" + "b" * 29
        suggestions = self.client.get("/api/auth/username-suggestions",
                                      params={"username": name}).json()["suggestions"]
        self.assertEqual(len(suggestions), 3)
        self.assertTrue(all(3 <= len(v) <= 30 for v in suggestions))
        self.assertEqual(self.client.get("/api/auth/username-suggestions",
                                         params={"username": "Вася"}).status_code, 422)

    def test_bonus_requires_login_and_is_idempotent_after_reload(self):
        self.assertEqual(self.client.post("/api/v2/stars/about-bonus").status_code, 401)
        self.assertEqual(self.signup("Alice").status_code, 201)
        self.assertEqual(self.client.get("/api/v2/stars/about-bonus").json(), {"claimed": False})
        first = self.client.post("/api/v2/stars/about-bonus")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["newly_claimed"], True)
        self.assertEqual(first.json()["balances"],
                         {"bronze": 50, "silver": 40, "gold": 30, "platinum": 20})
        again = self.client.post("/api/v2/stars/about-bonus")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertFalse(again.json()["newly_claimed"])
        self.assertEqual(again.json()["balances"], first.json()["balances"])
        self.assertEqual(self.client.get("/api/v2/stars/about-bonus").json(), {"claimed": True})
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 4)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertEqual(self.signup("Bob").status_code, 201)
        self.assertEqual(self.client.get("/api/v2/stars/about-bonus").json(), {"claimed": False})
        self.assertTrue(self.client.post("/api/v2/stars/about-bonus").json()["newly_claimed"])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(WalletTransaction.id))), 8)


if __name__ == "__main__":
    unittest.main()
