"""AdSense information pages' public contact endpoint: abuse, routing, SMTP safety."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.contact_routes import ContactMessage, send_contact_email
from persistence.database import get_db
from persistence.models import Base


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)

        def override_db():
            with Session(self.engine, autoflush=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(self.engine.dispose)
        self.enterContext(patch.dict("os.environ", {
            "AUTH_SMTP_HOST": "smtp.test",
            "AUTH_SMTP_PORT": "2525",
            "AUTH_FROM_EMAIL": "no-reply@example.com",
            "AUTH_PUBLIC_URL": "http://localhost:4200",
            "CONTACT_RECIPIENT_EMAIL": "operator@example.com",
        }))
        self.sent = []
        self.enterContext(patch(
            "persistence.contact_routes.send_contact_email",
            side_effect=lambda message: self.sent.append(message)))
        self.client = TestClient(app)
        self.payload = {"name": "Friendly player", "email": "person@example.net",
                        "message": "I enjoy the quiz.", "website": ""}

    def test_public_anonymous_contact_does_not_create_account_or_store_message(self):
        res = self.client.post("/api/contact", json=self.payload)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json(), {"sent": True})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].message, "I enjoy the quiz.")

    def test_honeypot_quietly_discarded(self):
        res = self.client.post("/api/contact", json={
            **self.payload, "website": "spam.example"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(self.sent)

    def test_throttled_per_client_using_shared_db_rate_bucket(self):
        for _ in range(4):
            self.assertEqual(self.client.post("/api/contact", json=self.payload).status_code, 200)
        self.assertEqual(self.client.post("/api/contact", json=self.payload).status_code, 429)
        self.assertEqual(len(self.sent), 4)

    def test_cross_site_and_malformed_payloads_rejected(self):
        self.assertEqual(self.client.post("/api/contact", json=self.payload,
            headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/contact", json=self.payload,
            headers={"sec-fetch-site": "cross-site"}).status_code, 403)
        for field, value in [
            ("message", ""), ("message", "a" * 2001),
            ("email", "not-an-email"), ("name", "bad\nheader"),
        ]:
            self.assertNotEqual(self.client.post("/api/contact", json={
                **self.payload, field: value}).status_code, 200)
        self.assertFalse(self.sent)

    def test_sender_cannot_choose_recipient_or_from_header(self):
        self.assertEqual(self.client.post("/api/contact", json={
            **self.payload, "recipient": "third-party@example.com",
        }).status_code, 422)
        self.assertFalse(self.sent)


class ContactSmtpTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {
            "AUTH_SMTP_HOST": "smtp.test", "AUTH_SMTP_PORT": "2525",
            "AUTH_SMTP_USERNAME": "smtp-user",
            "AUTH_SMTP_PASSWORD": "smtp-password",
            "AUTH_FROM_EMAIL": "no-reply@example.com",
            "AUTH_PUBLIC_URL": "https://quiz.example",
            "CONTACT_RECIPIENT_EMAIL": "operator@example.com",
        }))
        self.payload = ContactMessage(
            name="Alex", email="alex@example.net", message="A quick hello", website="")

    def test_real_smtp_message_is_addressed_only_to_configured_operator(self):
        sent = []
        class FakeSmtp:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def starttls(self, **kwargs): pass
            def login(self, username, password):
                self_credentials = (username, password)
                self.assertEqual(self_credentials, ("smtp-user", "smtp-password"))
            def send_message(self, message): sent.append(message)
        fake = FakeSmtp()
        with patch("persistence.contact_routes.smtplib.SMTP", return_value=fake):
            send_contact_email(self.payload)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["From"], "no-reply@example.com")
        self.assertEqual(sent[0]["To"], "operator@example.com")
        self.assertEqual(sent[0]["Reply-To"], "alex@example.net")
        self.assertNotIn("smtp-password", str(sent[0]))

    def test_missing_recipient_fails_closed(self):
        from fastapi import HTTPException
        with patch.dict("os.environ", {"CONTACT_RECIPIENT_EMAIL": ""}):
            with self.assertRaises(HTTPException) as ctx:
                send_contact_email(self.payload)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
