# AI Quiz Game — Premium / Buy Me a Coffee integration

## Status and current limits

The signed membership webhook and game entitlements are implemented in this branch.
**Do not enable checkout before migrating and configuring the webhook.**
The Bronze Star Shop is implemented but OFF by default. A real checkout is not
deployed or accepting money until five BMC Extras, their exact IDs and URLs,
a signed Shop webhook, and the **Special price for members** field set
to 20% off on each Shop item. BMC automatically applies this member price
for buyers it recognizes as members; there is no coupon code.
The game delivers confirmed purchases at exactly either the regular
or BMC member price. Read docs/BRONZE_STAR_SHOP.md.
Do not advertise Shop purchasing as ready until the signed purchase/refund
webhooks and a controlled real order have been verified. The coffee donation button remains separate and grants no rewards.

## Existing Premium rules

- Monthly membership: **$5**; yearly membership: **$50** (configured on Buy Me a Coffee).
- 8 free complaints per UTC calendar month, with no carryover.
- Premium conversion: 10 Gold -> 1 Platinum; never reverse. Platinum stays
  in the balance after Premium expires.
- Quality preference: among eligible, fully reviewed pool questions of the
  same requested difficulty, prefer higher community-rated questions. Do not
  bypass validation, deduplication or the global difficulty gate.
- Intended 20% reduction on purchased Bronze packs, implemented in
  `discounted_price_cents()` for a server-computed Shop quote. The Shop
  catalog and signed webhook endpoints are staged and disabled by default.
  A real BMC checkout requires the owner's verified Shop Extras and
  configured regular and member prices.

## Deploy (VPS)

1. Back up the PostgreSQL volume without touching the existing users and run
   `docker compose run --rm backend python -m alembic upgrade head`
   from the project root. This includes the staged 0018 Shop receipt tables
   if not already applied. Verify with `alembic current`.
2. Buy Me a Coffee creator dashboard → Integrations → New webhook:
   - URL: `https://squizthebot.com/api/v2/premium/webhook`
   - Events: `membership.started`, `membership.updated`,
     `membership.cancelled`, `membership.paused`.
   - Use the **Premium account** membership level, not general donations.
   - Copy the webhook signing secret from this webhook's detail page.
     Find the numeric `membership_level_id` in the signed event preview.
3. In the **private** Quiz Compose `.env`, set:
   ```dotenv
   BMC_WEBHOOK_SIGNING_SECRET=<the signing secret copied from the BMC webhook>
   BMC_PREMIUM_LEVEL_ID=<numeric Premium membership_level_id>
   ```
   Do not paste the secret into chat, GitHub, frontend code or logs.
   **Keep the new webhook disabled** until the server has been restarted
   with those settings (otherwise repeated 404/503 deliveries may disable it).
4. Recreate only Quiz backend and web after merging/deploying:
   `docker compose up -d --build backend web`.
   Check `GET /api/health`. Never run `docker compose down -v`.
5. Test via dashboard **Send test event**. Signed dashboard test events use
   `live_mode=false`: endpoint acknowledges them but **never activates
   Premium**. Confirm `GET /api/v2/premium` returns `active=false`.
   A real membership payment is required for a full end-to-end check.
6. Subscribe using the **same verified email** as the AI Quiz account.
   After a real signed event with the configured level ID, reload the game:
   `GET /api/v2/premium` should show `active=true`, 8 free appeals and
   20% as the *future star-shop discount policy*. Do not interpret these
   account-status values as a processed Shop order.
7. Verify one-way exchange with sufficient Gold. Repeated request IDs are
   idempotent; other conversion directions are refused. Paid cancellation
   retains benefits only until the provider's paid period end; pause disables
   immediately.

## Security and matching

- HMAC-SHA256 on **raw body** using `x-signature-sha256` is mandatory.
- `live_mode=false`, donation, recurring-donation, and unrelated tiers
  never create benefits.
- New subscriptions match `supporter_email` to an **already verified**
  login email; never match on display name.
- On subsequent events, the provider subscription ID and supporter ID are
  immutably bound to that same player.
- Every processed `event_id` is tracked in PostgreSQL for replay safety.
- Unknown buyer emails are acknowledged but **not assigned to another user**.
  Handle those purchases manually after investigating ownership; do not
  blindly attach by name or let players claim another person's order.
- Store no credit card/payment credentials in Quiz.
- The frontend checkout control remains disabled if signing secret or numeric
  Premium membership level ID is missing.

## Documentation

Official Buy Me a Coffee webhook docs (July 2026):
https://help.buymeacoffee.com/en/articles/15743173-how-to-setup-and-use-buy-me-a-coffee-webhooks

Official JSON schema:
https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json
