# AI Quiz Game — Monetization specification

Status: **Draft**, with individually approved rules identified below.
Last updated: 2026-11-16.

## Payment platform (planned)

Buy Me a Coffee is the proposed external checkout:
- Creator profile: https://buymeacoffee.com/subuday
- **Memberships**: subscription-based Premium.
- **Shop**: possible one-time star packs, subject to confirmation of platform policy.
- AI Quiz Game maintains entitlements, balances, and transaction history in PostgreSQL, not in the payment provider.
- Provider webhook integration, purchase-to-player binding, payment verification, refunds, and payout readiness are **not yet implemented or validated**.
- Donations must not automatically grant stars or Premium.

## Premium: approved conversion perk

**Decision approved by the owner on 2026-11-16:**
- A player with **active Premium** may convert **10 Gold stars into 1 Platinum star**.
- Conversion is **one-way only**: **Gold → Platinum**.
- **Platinum → Gold is forbidden**, including for Premium members.
- Players without active Premium cannot initiate this conversion.
- A conversion must require at least 10 Gold and explicitly show the rate and resulting balances before confirmation.
- Deduct 10 Gold and credit 1 Platinum in a **single atomic PostgreSQL transaction**, with server-side validation, concurrency safety, and an auditable conversion record. Prevent duplicate application of the same request.
- Platinum already obtained remains in the player's balance when Premium expires or is cancelled. Expiration merely removes the ability to initiate *new* conversions.
- Conversion is not a hint and has no impact on bonus-question eligibility, unless separately specified.

This is a **product requirement**, not a claim that the conversion is already implemented.

## Other monetization proposals — not yet approved

- Premium subscription monthly/yearly prices and benefits other than the conversion perk.
- Star pack denominations, quantities, and prices.
- How purchase identifiers are reliably tied to game accounts.
- Payment provider policy clearance for sale of in-game currency.
- Refund/chargeback and subscription-lifecycle rules.

Do not publish or enable paid products until the relevant items are designed, policy-checked, implemented, and tested.
