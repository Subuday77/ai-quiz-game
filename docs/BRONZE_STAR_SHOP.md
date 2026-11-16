# Bronze Star Shop — Buy Me a Coffee

The feature is **OFF by default**. A donation, membership or unrecognised
Shop Extra can never credit stars. Only signed BMC Shop purchase notifications
for the allowlisted product ID may credit one bronze pack.

## Catalog

| Bronze | USD | Premium 20% discount |
|---:|---:|---:|
| 30 | $3.00 | $2.40 |
| 100 | $9.00 | $7.20 |
| 250 | $20.00 | $16.00 |
| 500 | $35.00 | $28.00 |
| 1,000 | $60.00 | $48.00 |

The game continues to support bronze:silver:gold exchange 10:1 and
Premium-only 10 gold -> 1 platinum. New About rewards are 5 bronze, 4 silver,
3 gold and 2 platinum. Existing wallet credits are **never taken back**;
About reward claim idempotency keys are unchanged.

## Provider setup

1. In the Buy Me a Coffee page /subuday, create exactly five Shop Extras,
   priced in USD as above. If possible disable multiple quantities, variable
   pricing and unsolicited tips for these items. Record each actual Shop
   Extra ID from provider event payload and each real checkout URL. **Do not
   invent IDs or URLs.**
2. Create a separate Shop webhook in BMC Integrations. Subscribe to events
   extra_purchase.created and extra_purchase.refunded and copy its signing
   secret. Do not reuse the existing Premium webhook secret.
3. Create a 20% Shop discount code for Premium players. Entered at BMC's
   checkout, this code is not automatically attached to AI Quiz Premium status.
   Coupon sharing is possible; the signed webhook verifies that a discounted
   receipt belongs to a verified account with currently active Premium.
   Non-Premium discounted orders go to manual review, so address coupon
   sharing before real sales.
4. Set these backend environment variables (illustrative placeholders only):

    BMC_STAR_SHOP_ENABLED=false
    BMC_STAR_WEBHOOK_SIGNING_SECRET=<secret>
    BMC_STAR_PREMIUM_COUPON=<real BMC Shop code>
    BMC_STAR_SHOP_ITEMS={"30":{"extra_id":<id>,"url":"<real url>"},"100":{"extra_id":<id>,"url":"<real url>"},"250":{"extra_id":<id>,"url":"<real url>"},"500":{"extra_id":<id>,"url":"<real url>"},"1000":{"extra_id":<id>,"url":"<real url>"}}

   IDs must be unique positive integers; links must be valid HTTPS
   buymeacoffee.com/subuday/ links. An incomplete catalog disables checkout.
5. **Take a PostgreSQL backup and apply Alembic migration 0018_star_shop
   before running the new backend.** Never use Docker to reset the database.
6. With BMC_STAR_SHOP_ENABLED=false, deploy backend/frontend and verify
   GET /api/v2/star-shop returns the five prices with ready=false and
   checkout_url=null for every product; no live purchase can be processed.
7. Use the provider test event tool to verify that signed test deliveries
   have zero credits and invalid signatures return 401. Confirm receipt
   validation and idempotence in staging.
8. Enable the BMC Shop webhook and BMC_STAR_SHOP_ENABLED=true only after
   actual five item IDs, prices and the Premium code have been verified.
   Complete a controlled real purchase and refund test before announcement.

## Security and reconciliation

- Backend endpoint: POST /api/v2/star-shop/webhook.
- All live events must have HMAC-SHA256 of the *raw* body in the
  x-signature-sha256 header and live_mode=true.
- Exactly one Shop Extra in an order; quantity=1; currency=USD; payment
  succeeded and not refunded; its configured ID, exact USD amount and
  verified game email must all match. Mixed orders, unmatched emails,
  invalid amounts/status and unrecognised IDs are not auto-credited.
- Provider purchase ID and event ID are both unique PostgreSQL boundaries.
  Credit and receipt are committed together under a per-user row lock.
- Test events, coffee donations, memberships, Shop updates and retries
  can never create a second star credit.
- A signed refund reverses the credit **only if the purchased bronze balance
  remains available**. Otherwise it records refund_pending for operator
  reconciliation without creating a negative in-game balance. A refund
  arriving before the purchase is tombstoned to block later credit.
- No raw payment payload or buyer email is stored in the receipt table.
  For unmatched and needs_review receipts, use BMC's event delivery
  dashboard and the purchase ID for manual resolution.
- Checkout links are supplied by trusted server environment configuration;
  browser never submits amount, payment status, or user ID as evidence.

Official BMC docs:
https://help.buymeacoffee.com/en/articles/15743173-how-to-setup-and-use-buy-me-a-coffee-webhooks
https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json
https://help.buymeacoffee.com/en/articles/9403699-how-do-you-add-discounts-on-your-shop-items
