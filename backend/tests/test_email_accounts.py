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

    def test_new_account_keeps_selected_ui_language_after_email_verification(self):
        created = self.client.post("/api/auth/email/register", json={
            "email": "ru-player@example.com",
            "display_name": "Тестовый игрок",
            "password": "correct horse battery staple",
            "ui_language": "ru",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["ui_language"], "ru")
        self.assertFalse(created.json()["email_verified"])
        self.assertEqual(self.client.get("/api/auth/me").json()["ui_language"], "ru")
        self.assertEqual(len(self.sent), 1)

        verified = self.client.post("/api/auth/email/verify", json={
            "token": self.sent[0][1],
        })
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(self.client.get("/api/auth/me").json()["email_verified"])
        self.assertEqual(self.client.get("/api/auth/me").json()["ui_language"], "ru")

        self.client.post("/api/auth/logout")
        signed_in_again = self.client.post("/api/auth/email/login", json={
            "email": "ru-player@example.com",
            "password": "correct horse battery staple",
        })
        self.assertEqual(signed_in_again.status_code, 200, signed_in_again.text)
        self.assertEqual(signed_in_again.json()["ui_language"], "ru")

    def test_registration_defaults_to_english_and_rejects_unsupported_ui_languages(self):
        invalid = self.client.post("/api/auth/email/register", json={
            "email": "invalid-locale@example.com",
            "display_name": "A",
            "password": "correct horse battery staple",
            "ui_language": "not-supported",
        })
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(self.sent, [])

        legacy = self.register("default-locale@example.com")
        self.assertEqual(legacy.status_code, 201, legacy.text)
        self.assertEqual(legacy.json()["ui_language"], "en")


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

    def test_game_blocked_until_email_verified_with_profile_recovery(self):
        created = self.register("typo@example.com")
        self.assertEqual(created.status_code, 201, created.text)
        self.assertFalse(created.json()["email_verified"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.client.get("/api/v2/games/active").status_code, 403)
        self.assertEqual(self.client.post("/api/v2/games", json={
            "language": "English",
        }).status_code, 403)

        original_token = self.sent[-1][1]
        resend = self.client.post("/api/auth/email/resend", json={})
        self.assertEqual(resend.status_code, 200, resend.text)
        self.assertNotEqual(self.sent[-1][1], original_token)
        wrong_password = self.client.post("/api/auth/email/replace-unverified", json={
            "email": "correct@example.com", "current_password": "wrong-password",
        })
        self.assertEqual(wrong_password.status_code, 401)
        corrected = self.client.post("/api/auth/email/replace-unverified", json={
            "email": "correct@example.com",
            "current_password": "correct horse battery staple",
        })
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertFalse(corrected.json()["email_verified"])
        self.assertEqual(self.client.post("/api/auth/email/verify", json={
            "token": original_token,
        }).status_code, 400)
        self.assertEqual(self.client.get("/api/v2/games/active").status_code, 403)

        verified = self.client.post("/api/auth/email/verify", json={
            "token": self.sent[-1][1],
        })
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(self.client.get("/api/auth/me").json()["email_verified"])
        self.assertEqual(self.client.get("/api/v2/games/active").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/email/resend", json={}).status_code, 409)

    def test_verify_link_in_other_players_browser_does_not_switch_session(self):
        created = self.register("first@example.com")
        self.assertEqual(created.status_code, 201, created.text)
        first_id = created.json()["id"]
        verify_token = self.sent[-1][1]

        other_browser = TestClient(app)
        self.addCleanup(other_browser.close)
        other = other_browser.post("/api/auth/register", json={
            "username": "OtherPlayer", "password": "other-player-password-123",
        })
        self.assertEqual(other.status_code, 201, other.text)
        other_id = other.json()["id"]

        confirmed = other_browser.post("/api/auth/email/verify", json={
            "token": verify_token,
        })
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["user_id"], first_id)
        self.assertEqual(confirmed.json()["email"], "first@example.com")
        # Mailbox confirmation never changes who is signed into this browser.
        current = other_browser.get("/api/auth/me")
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["id"], other_id)
        # The original browser keeps its own session and is now verified.
        original = self.client.get("/api/auth/me")
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.json()["id"], first_id)
        self.assertTrue(original.json()["email_verified"])

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

    def test_forgot_password_rate_limit_ignores_spoofed_forwarded_for(self):
        for index in range(20):
            response = self.client.post("/api/auth/password/forgot",
                json={"email": "unknown@example.com"},
                headers={"X-Forwarded-For": f"192.0.2.{index + 1}"})
            self.assertEqual(response.status_code, 200, response.text)
        blocked = self.client.post("/api/auth/password/forgot",
            json={"email": "unknown@example.com"},
            headers={"X-Forwarded-For": "203.0.113.99"})
        self.assertEqual(blocked.status_code, 429)

    def test_missing_smtp_secret_fails_before_account_creation(self):
        with patch.dict("os.environ", {
            "AUTH_SMTP_USERNAME": "mail-user",
            "AUTH_SMTP_PASSWORD": "",
        }):
            response = self.register("unsent@example.com")
            self.assertEqual(response.status_code, 503, response.text)
            with Session(self.engine) as db:
                self.assertIsNone(db.scalar(
                    select(User).where(User.email == "unsent@example.com")
                ))

    def test_unverified_emails_cannot_reset(self):
        self.register("noverify@example.com")
        n = len(self.sent)
        result = self.client.post("/api/auth/password/forgot", json={"email": "noverify@example.com"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.sent), n)



class SMTPTransportTests(unittest.TestCase):
    def test_brevo_port_2525_uses_starttls(self):
        from persistence.account_email_routes import send_account_email
        values = {
            "AUTH_PUBLIC_URL": "https://squizthebot.com",
            "AUTH_FROM_EMAIL": "no-reply@squizthebot.com",
            "AUTH_SMTP_HOST": "smtp-relay.brevo.com",
            "AUTH_SMTP_PORT": "2525",
            "AUTH_SMTP_USERNAME": "test@smtp-brevo.com",
            "AUTH_SMTP_PASSWORD": "test-secret",
        }
        with patch.dict("os.environ", values), patch(
            "persistence.account_email_routes.smtplib.SMTP"
        ) as smtp_class:
            send_account_email("recipient@example.com", "test-token", purpose="verify_email")
            smtp_class.assert_called_once_with("smtp-relay.brevo.com", 2525, timeout=10)
            client = smtp_class.return_value.__enter__.return_value
            client.starttls.assert_called_once()
            client.login.assert_called_once_with("test@smtp-brevo.com", "test-secret")
            message = client.send_message.call_args.args[0]
            self.assertEqual(message["From"], "no-reply@squizthebot.com")
            self.assertIn("https://squizthebot.com/#verify_email=test-token",
                          message.get_content())


if __name__ == "__main__":
    unittest.main()
