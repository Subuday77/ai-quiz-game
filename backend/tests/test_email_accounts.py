"""Email-first auth regression tests, including migration of a legacy account."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.account_email import token_digest
from persistence.database import get_db
from persistence.models import AccountEmailToken, AuthSession, Base, User


class EmailAccountFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.dict("os.environ", {
            "AUTH_COOKIE_SECURE": "false",
            "AUTH_SMTP_HOST": "localhost",
            "AUTH_FROM_EMAIL": "test@example.com",
            "AUTH_PUBLIC_URL": "http://localhost:4200",
        }))
        self.sent = []
        self.enterContext(patch("persistence.account_email_routes.send_account_email",
            side_effect=lambda email, token, purpose: self.sent.append((email, token, purpose))))
        self.client = TestClient(app)

    def register(self, email, display_name="Alex"):
        return self.client.post("/api/auth/email/register", json={
            "email": email, "display_name": display_name,
            "password": "correct horse battery staple",
        })

    def test_duplicate_display_names_distinct_ids_and_unique_emails(self):
        first = self.register("first@example.com")
        self.assertEqual(first.status_code, 201, first.text)
        first_id = first.json()["id"]
        self.client.post("/api/auth/logout")
        second = self.register("second@example.com")
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotEqual(first_id, second.json()["id"])
        self.assertEqual(first.json()["display_name"], second.json()["display_name"])
        self.assertEqual(self.register("FIRST@EXAMPLE.COM").status_code, 409)

    def test_verify_and_reset_revoke_sessions(self):
        first = self.register("first@example.com")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertFalse(first.json()["email_verified"])
        token = self.sent[-1][1]
        verified = self.client.post("/api/auth/email/verify", json={"token": token})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(self.client.post("/api/auth/email/verify", json={"token": token}).status_code, 400)
        self.assertTrue(self.client.get("/api/auth/me").json()["email_verified"])
        forgotten = self.client.post("/api/auth/password/forgot", json={"email": "FIRST@example.com"})
        unknown = self.client.post("/api/auth/password/forgot", json={"email": "no-one@example.com"})
        self.assertEqual(forgotten.json(), unknown.json())
        reset_token = self.sent[-1][1]
        # A second link for the same account must be revoked after one succeeds.
        self.client.post("/api/auth/password/forgot", json={"email": "first@example.com"})
        second_token = self.sent[-1][1]
        changed = self.client.post("/api/auth/password/reset", json={
            "token": reset_token, "new_password": "new corrected password!"
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/password/reset", json={
            "token": reset_token, "new_password": "different good password"
        }).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/password/reset", json={
            "token": second_token, "new_password": "another adequate password"
        }).status_code, 400)
        signed = self.client.post("/api/auth/email/login", json={
            "email": "FIRST@example.com", "password": "new corrected password!"
        })
        self.assertEqual(signed.status_code, 200, signed.text)

    def test_legacy_user_claim_preserves_uuid(self):
        created = self.client.post("/api/auth/register", json={
            "username": "Legacy", "password": "legacy-password-123"
        })
        self.assertEqual(created.status_code, 201, created.text)
        old_id = created.json()["id"]
        claimed = self.client.post("/api/auth/email/claim", json={"email": "legacy@example.com"})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        token = self.sent[-1][1]
        self.assertEqual(self.client.post("/api/auth/email/verify", json={"token": token}).status_code, 200)
        self.client.post("/api/auth/logout")
        new_login = self.client.post("/api/auth/email/login", json={
            "email": "legacy@example.com", "password": "legacy-password-123"
        })
        self.assertEqual(new_login.status_code, 200, new_login.text)
        self.assertEqual(new_login.json()["id"], old_id)
        renamed = self.client.patch("/api/auth/display-name", json={"display_name": "Alex"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["id"], old_id)

    def test_correct_unverified_email_invalidates_old_link(self):
        original = self.register("typo@example.com")
        self.assertEqual(original.status_code, 201, original.text)
        original_id = original.json()["id"]
        old_token = self.sent[-1][1]
        wrong_password = self.client.post("/api/auth/email/replace-unverified", json={
            "email": "correct@example.com", "current_password": "wrong-password"
        })
        self.assertEqual(wrong_password.status_code, 401)
        corrected = self.client.post("/api/auth/email/replace-unverified", json={
            "email": "correct@example.com", "current_password": "correct horse battery staple"
        })
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["id"], original_id)
        self.assertEqual(corrected.json()["email"], "correct@example.com")
        self.assertEqual(self.client.post("/api/auth/email/verify", json={"token": old_token}).status_code, 400)
        new_token = self.sent[-1][1]
        self.assertEqual(self.client.post("/api/auth/email/verify", json={"token": new_token}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/email/replace-unverified", json={
            "email": "another@example.com", "current_password": "correct horse battery staple"
        }).status_code, 409)

    def test_unverified_emails_cannot_reset(self):
        self.register("noverify@example.com")
        n = len(self.sent)
        result = self.client.post("/api/auth/password/forgot", json={"email": "noverify@example.com"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.sent), n)


if __name__ == "__main__":
    unittest.main()
