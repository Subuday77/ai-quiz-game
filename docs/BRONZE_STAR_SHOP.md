# Bronze Star Shop — Buy Me a Coffee

> **Compliance hold / DRAFT ONLY.** No items have been published or saved in
> the BMC editor by this PR, no payments are accepted and the PR must not be
> merged as a live shop. Written BMC policy confirmation is needed regarding
> non-cash game currency and the optional bonus-question star stake. The
> BMC Shop currently displays an unlisted/identity-verification warning.
> See [the five listing drafts and support request](BRONZE_SHOP_LISTINGS_DRAFT.md).
> Sending the prepared support email is a separate owner decision.

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
3. For **each Shop Extra**, open Advanced settings and enable
   **Special price for members**. Enter its member price from the table
   above (20% lower than normal). No discount coupon is needed. Leave
   **Allow buyer to choose a quantity** OFF. Buy Me a Coffee decides
   which buyer qualifies for member pricing at checkout; the Quiz game
   displays the Premium quote but never overrides the provider's decision.
   Ensure the Buy Me a Coffee membership and Quiz registration use the
   same verified email. Verify the actual paid amount in a signed Shop
   event before enabling purchases.
4. Set these backend environment variables (illustrative placeholders only):

    BMC_STAR_SHOP_ENABLED=false
    BMC_STAR_WEBHOOK_SIGNING_SECRET=<secret>
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
8. **Do not enable live payments before written BMC policy clearance**, verification
   of the creator's identity and Shop publication eligibility, successful CI,
   backup/migration, and confirmation of the real five item IDs and both price
   fields (regular/member). Complete controlled real purchase and refund
   checks before public announcement.

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
- A signed successful Shop payment is eligible only at the exact full price
  or exact built-in member price; neither the browser nor any coupon is
  accepted as evidence of payment. If BMC recognizes someone as a member
  even while Quiz membership sync is delayed, the verified paid order is
  still fulfilled without taking money and withholding stars.
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

Policy references:
https://help.buymeacoffee.com/en/articles/3364212-what-do-we-restrict-on-buy-me-a-coffee
https://buymeacoffee.com/community-guidelines
https://help.buymeacoffee.com/en/articles/9037311-product-guidelines-for-buy-me-a-coffee-shop
