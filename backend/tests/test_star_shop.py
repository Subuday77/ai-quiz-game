"""Offline BMC Shop security, receipt, refund and pricing tests."""
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import (
    Base, PremiumMembership, StarShopPurchase, StarShopWebhookEvent, WalletTransaction,
)
from persistence.star_shop import PACK_PRICES, _money_cents

SECRET = "shop-ci-signing-only"
PRODUCTS = {
    str(count): {"extra_id": 1000 + count, "url":
                 "https://buymeacoffee.com/subuday/e/" + str(1000 + count)}
    for count in PACK_PRICES
}


class StarShopTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.enterContext(patch.dict("os.environ", {
            "AUTH_COOKIE_SECURE": "false",
            "AUTH_SMTP_HOST": "localhost", "AUTH_FROM_EMAIL": "ci@example.com",
            "AUTH_PUBLIC_URL": "http://localhost:4200",
            "BMC_STAR_SHOP_ENABLED": "true",
            "BMC_STAR_WEBHOOK_SIGNING_SECRET": SECRET,
            "BMC_STAR_SHOP_ITEMS": json.dumps(PRODUCTS),
        }))
        self.sent = []
        self.enterContext(patch(
            "persistence.account_email_routes.send_account_email",
            side_effect=lambda email, token, purpose: self.sent.append((email, token, purpose)),
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        created = self.client.post("/api/auth/email/register", json={
            "email": "buyer@example.com", "display_name": "Star Buyer",
            "password": "some-very-safe-password",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.user_id = UUID(created.json()["id"])
        verified = self.client.post("/api/auth/email/verify",
                                    json={"token": self.sent[-1][1]})
        self.assertEqual(verified.status_code, 200, verified.text)

    def payment(self, *, event_id=701, purchase_id=9001,
                kind="extra_purchase.created", count=30,
                amount=3.0, email="buyer@example.com", live=True,
                quantity=1, status="succeeded", refunded="false",
                product_id=None, extras=None, currency="USD"):
        if extras is None:
            extras = [{
                "id": product_id if product_id is not None else 1000 + count,
                "object": "extra", "title": "Bronze stars",
                "quantity": quantity, "currency": currency, "amount": amount,
            }]
        envelope = {
            "event_id": event_id, "type": kind, "live_mode": live,
            "created": int(datetime.now(timezone.utc).timestamp()),
            "attempt": 1,
            "data": {
                "id": purchase_id, "object": "payment",
                "status": status, "refunded": refunded,
                "support_type": "Extra", "currency": currency,
                "amount": amount, "supporter_email": email,
                "supporter_id": 77, "extras": extras,
            },
        }
        body = json.dumps(envelope).encode("utf-8")
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return body, sig

    def send(self, **kwargs):
        body, sig = self.payment(**kwargs)
        return self.client.post("/api/v2/star-shop/webhook", content=body, headers={
            "x-signature-sha256": sig, "content-type": "application/json",
        })

    def balance(self):
        response = self.client.get("/api/v2/stars")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["bronze"]

    def orders(self):
        with Session(self.engine) as db:
            return list(db.scalars(select(StarShopPurchase)))

    def test_shop_catalog_and_activation_gate(self):
        catalog = self.client.get("/api/v2/star-shop")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertTrue(catalog.json()["ready"])
        self.assertEqual(catalog.json()["verified_email"], "buyer@example.com")
        self.assertEqual([(p["bronze"], p["price_cents"]) for p in catalog.json()["packages"]],
                         list(PACK_PRICES.items()))
        with patch.dict("os.environ", {"BMC_STAR_SHOP_ENABLED": "false"}):
            disabled = self.client.get("/api/v2/star-shop").json()
            self.assertFalse(disabled["ready"])
            self.assertTrue(all(p["checkout_url"] is None for p in disabled["packages"]))
            self.assertEqual(self.send().status_code, 503)

    def test_signature_test_event_and_donation_never_credit(self):
        body, _ = self.payment()
        response = self.client.post("/api/v2/star-shop/webhook", content=body,
                                    headers={"x-signature-sha256": "0" * 64})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.send(event_id=1, live=False).json()["applied"])
        self.assertFalse(self.send(event_id=2, kind="donation.created").json()["applied"])
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.orders(), [])

    def test_exactly_once_even_when_event_id_changes(self):
        first = self.send()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["applied"])
        self.assertEqual(self.balance(), 30)
        self.assertTrue(self.send().json()["duplicate"])
        second_event = self.send(event_id=702)
        self.assertFalse(second_event.json()["applied"])
        self.assertEqual(self.balance(), 30)
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.orders()[0].status, "credited")
        with Session(self.engine) as db:
            self.assertEqual(len(list(db.scalars(select(StarShopWebhookEvent)))), 2)

    def test_strict_price_extra_currency_quantity_and_verified_email(self):
        scenarios = [
            {"amount": 2.9}, {"amount": 3.5},
            {"product_id": 987654}, {"quantity": 2}, {"currency": "EUR"},
            {"status": "pending"}, {"refunded": "true"},
            {"email": "unknown@example.com"},
            {"extras": []},
            {"extras": [
                {"id": 1030, "object": "extra", "quantity": 1, "currency": "USD"},
                {"id": 1100, "object": "extra", "quantity": 1, "currency": "USD"},
            ]},
        ]
        for index, changes in enumerate(scenarios):
            with self.subTest(changes=changes):
                response = self.send(event_id=810 + index, purchase_id=10010 + index,
                                     **changes)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["applied"])
        self.assertEqual(self.balance(), 0)
        self.assertTrue(all(row.status in ("needs_review", "unmatched") for row in self.orders()))

    def test_successful_refund_reverses_once(self):
        self.assertTrue(self.send()["applied"])
        refunded = self.send(event_id=730, kind="extra_purchase.refunded")
        self.assertEqual(refunded.status_code, 200, refunded.text)
        self.assertTrue(refunded.json()["applied"])
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.orders()[0].status, "refunded")
        self.assertFalse(self.send(event_id=731, kind="extra_purchase.refunded").json()["applied"])
        self.assertEqual(self.balance(), 0)

    def test_refund_before_creation_is_tombstoned(self):
        self.assertEqual(self.send(event_id=740, purchase_id=9999,
                                   kind="extra_purchase.refunded").status_code, 200)
        self.assertEqual(self.orders()[0].status, "refunded_before_credit")
        response = self.send(event_id=741, purchase_id=9999)
        self.assertFalse(response.json()["applied"])
        self.assertEqual(self.balance(), 0)

    def test_spent_stars_do_not_corrupt_wallet_on_refund(self):
        self.assertTrue(self.send()["applied"])
        with Session(self.engine) as db:
            db.add(WalletTransaction(
                user_id=self.user_id, amount=-25, reason="star/bronze/qa",
                idempotency_key="shop-refund-spent-qa",
            ))
            db.commit()
        response = self.send(event_id=750, kind="extra_purchase.refunded")
        self.assertFalse(response.json()["applied"])
        self.assertEqual(self.orders()[0].status, "refund_pending")
        self.assertEqual(self.balance(), 5)

    def test_provider_member_price_delivers_to_verified_buyer(self):
        discounted = self.send(event_id=876, purchase_id=5555, amount=2.4)
        self.assertEqual(discounted.status_code, 200, discounted.text)
        self.assertTrue(discounted.json()["applied"])
        self.assertEqual(self.balance(), 30)

    def test_bmc_member_price_is_quoted_to_active_premium(self):
        with Session(self.engine) as db:
            db.add(PremiumMembership(
                user_id=self.user_id, provider="buymeacoffee",
                provider_subscription_id="paid-premium-test",
                provider_supporter_id="77", status="active", plan="month",
                current_period_end=datetime.now(timezone.utc) + timedelta(days=30),
                last_event_created=datetime.now(timezone.utc), last_event_id=1001,
            ))
            db.commit()
        catalog = self.client.get("/api/v2/star-shop").json()
        self.assertTrue(catalog["premium"])
        self.assertTrue(catalog["ready"])
        self.assertEqual(catalog["packages"][0]["price_cents"], 240)
        response = self.send(event_id=888, purchase_id=7777, amount=2.4)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["applied"])
        # No discount code is needed: the provider's member pricing applies
        # at checkout, while our backend only validates the signed amount.
        self.assertNotIn("premium_coupon", catalog)
        self.assertTrue(all(p["checkout_url"] for p in catalog["packages"]))

    def test_money_parsing_does_not_round_or_accept_invalid_values(self):
        for raw in (True, None, {}, "oops", "2.999", 0, -1, 1e20):
            self.assertIsNone(_money_cents(raw))
        self.assertEqual(_money_cents("3.00"), 300)
        self.assertEqual(_money_cents("0.60"), 60)


if __name__ == "__main__":
    unittest.main()
