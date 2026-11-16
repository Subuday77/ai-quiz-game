"""Premium webhook, entitlement, exchange and quota regression (no paid calls)."""
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import Base, PremiumMembership, WalletTransaction

SECRET = "ci-only-premium-webhook-secret"


class PremiumTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                yield session
        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.enterContext(patch.dict("os.environ", {
            "AUTH_COOKIE_SECURE": "false",
            "AUTH_SMTP_HOST": "localhost", "AUTH_FROM_EMAIL": "ci@example.com",
            "AUTH_PUBLIC_URL": "http://localhost:4200",
            "BMC_WEBHOOK_SIGNING_SECRET": SECRET, "BMC_PREMIUM_LEVEL_ID": "77",
        }))
        self.sent = []
        self.enterContext(patch(
            "persistence.account_email_routes.send_account_email",
            side_effect=lambda email, token, purpose: self.sent.append((email, token, purpose)),
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        created = self.client.post("/api/auth/email/register", json={
            "email": "member@example.com", "display_name": "Subscriber",
            "password": "some-very-safe-password",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.user_id = created.json()["id"]
        verified = self.client.post("/api/auth/email/verify",
                                    json={"token": self.sent[-1][1]})
        self.assertEqual(verified.status_code, 200, verified.text)

    def event(self, event_id=100, event_type="membership.started",
              *, email="member@example.com", level=77, live=True,
              status="active", started_at=None, period_end=None):
        now = int(datetime.now(timezone.utc).timestamp())
        envelope = {
            "event_id": event_id,
            "type": event_type,
            "live_mode": live,
            "attempt": 1,
            "created": started_at or now,
            "data": {
                "id": 888, "object": "membership",
                "supporter_id": 123,
                "supporter_email": email,
                "membership_level_id": level,
                "membership_level_name": "Premium account",
                "duration_type": "month",
                "status": status,
                "current_period_end": period_end or now + 30 * 86400,
            },
        }
        raw = json.dumps(envelope).encode()
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return raw, sig

    def send_event(self, **kwargs):
        raw, sig = self.event(**kwargs)
        return self.client.post("/api/v2/premium/webhook", content=raw,
                                headers={"x-signature-sha256": sig,
                                         "content-type": "application/json"})

    def test_untrusted_or_test_events_never_grant(self):
        raw, _ = self.event()
        forged = self.client.post("/api/v2/premium/webhook", content=raw,
                                  headers={"x-signature-sha256": "0" * 64})
        self.assertEqual(forged.status_code, 401)
        self.assertFalse(self.client.get("/api/v2/premium").json()["active"])
        self.assertEqual(self.send_event(event_id=101, live=False).status_code, 200)
        self.assertEqual(self.send_event(event_id=102, level=12).status_code, 200)
        self.assertEqual(self.send_event(event_id=103, email="stranger@example.com").status_code, 200)
        self.assertFalse(self.client.get("/api/v2/premium").json()["active"])

    def test_membership_grants_and_retries_do_not_duplicate(self):
        initial = self.client.get("/api/v2/appeals/allowance").json()
        self.assertEqual(initial["free_total"], 5)
        created = self.send_event()
        self.assertEqual(created.status_code, 200, created.text)
        self.assertTrue(created.json()["applied"])
        duplicate = self.send_event()
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        premium = self.client.get("/api/v2/premium").json()
        self.assertTrue(premium["active"])
        self.assertEqual(premium["star_discount_percent"], 20)
        self.assertTrue(premium["priority_questions"])
        allowance = self.client.get("/api/v2/appeals/allowance").json()
        self.assertEqual(allowance["free_total"], 8)

    def test_cancelled_keeps_paid_period_paused_does_not(self):
        self.assertEqual(self.send_event().status_code, 200)
        later = int(datetime.now(timezone.utc).timestamp()) + 2
        cancel = self.send_event(event_id=104, event_type="membership.cancelled",
                                 status="canceled", started_at=later)
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertTrue(self.client.get("/api/v2/premium").json()["active"])
        pause = self.send_event(event_id=105, event_type="membership.paused",
                                status="paused", started_at=later + 2)
        self.assertEqual(pause.status_code, 200, pause.text)
        self.assertFalse(self.client.get("/api/v2/premium").json()["active"])

    def test_same_second_cancel_event_is_not_lost(self):
        timestamp = int(datetime.now(timezone.utc).timestamp())
        first = self.send_event(event_id=200, started_at=timestamp)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(self.client.get("/api/v2/premium").json()["active"])
        canceled = self.send_event(event_id=201, event_type="membership.paused",
                                   status="paused", started_at=timestamp)
        self.assertEqual(canceled.status_code, 200, canceled.text)
        self.assertFalse(self.client.get("/api/v2/premium").json()["active"])

    def test_premium_gold_to_platinum_atomic_and_one_way(self):
        self.assertEqual(self.send_event().status_code, 200)
        with Session(self.engine) as db:
            from uuid import UUID
            db.add(WalletTransaction(
                user_id=UUID(self.user_id), amount=10, reason="star/gold/qa",
                idempotency_key="qa-premium-ten-gold",
            ))
            db.commit()
        operation = {
            "source": "gold", "target": "platinum", "amount": 10,
            "request_id": str(uuid4()),
        }
        first = self.client.post("/api/v2/stars/premium/gold-to-platinum", json=operation)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["gold"], 0)
        self.assertEqual(first.json()["platinum"], 1)
        retry = self.client.post("/api/v2/stars/premium/gold-to-platinum", json=operation)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["platinum"], 1)
        reversed_exchange = self.client.post("/api/v2/stars/premium/gold-to-platinum", json={
            "source": "platinum", "target": "gold", "amount": 1,
            "request_id": str(uuid4()),
        })
        self.assertEqual(reversed_exchange.status_code, 422)

    def test_expired_membership_does_not_grant(self):
        now = int(datetime.now(timezone.utc).timestamp())
        self.assertEqual(self.send_event(period_end=now - 30).status_code, 200)
        self.assertFalse(self.client.get("/api/v2/premium").json()["active"])

    def test_price_quote_rounding_and_rejects_bad_values(self):
        from persistence.premium import discounted_price_cents
        self.assertEqual(discounted_price_cents(1000, active=True), 800)
        self.assertEqual(discounted_price_cents(999, active=True), 799)
        self.assertEqual(discounted_price_cents(999, active=False), 999)
        with self.assertRaises(ValueError):
            discounted_price_cents(-1, active=True)
