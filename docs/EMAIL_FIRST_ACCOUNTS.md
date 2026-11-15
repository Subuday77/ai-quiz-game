# AI Quiz Game — Email-first accounts

**Status: implemented in draft PR #38, NOT deployed or released.**
Updated: 2026-11-16.
Scope: account identity, password recovery, verified email and duplicate display names; no Buy Me a Coffee webhook.

## Preserve existing accounts

The repository already uses UUID `users.id` and all game/economy/appeal references link to this UUID. Keep all existing IDs intact. Existing Argon2id password hashes and database-backed browser sessions also remain intact.

Old usernames continue to be accepted in `POST /api/auth/login` during migration. The unique `username_key` is retained only as an internal legacy key; **do not remove it until no legacy login still depends on it**. New accounts get internal machine-generated usernames, but show a user-chosen `display_name` (nonunique, Unicode, 1–80 chars). Never use display names as login credentials.

## What PR #38 implements

- Migration `backend/migrations/versions/0015_email_accounts.py`: nullable case-insensitive canonical email, `email_verified_at` and one-time token records; no rekeying.
- Unique email canonicalization with IDNA domains and case-insensitive mailbox policy. No provider-specific dot or plus folding.
- `POST /api/auth/email/register` — verified-email flow begins for a new `email`, `display_name`, `password`.
- `POST /api/auth/email/login` — email/password sign-in; pending verification does **not** block game access but password recovery and external purchases require verification.
- `POST /api/auth/email/claim` — existing authenticated legacy user binds an email to the same UUID.
- `POST /api/auth/email/resend` — resend verification to a pending user.
- `POST /api/auth/email/replace-unverified` — correct a mistyped unverified email after verifying the current password; invalidates prior verification links.
- `POST /api/auth/email/verify` — redeem one-time verification.
- `POST /api/auth/password/forgot` — uniform outward response for present and absent emails; only verified addresses get mail.
- `POST /api/auth/password/reset` — one-time password reset; resets Argon2id credentials; invalidates all other reset links and all sessions.
- `PATCH /api/auth/display-name` — authenticated change of nonunique name, preserving UUID, stars and history.
- `GET /api/auth/me` reports verified-email state; legacy login remains operational.
- Angular account dialogs for new email registration/login, old username fallback, recovery, email verification, pending email correction and nonunique name.
- New canonical English localization keys; translations follow the normal AI Localizer pipeline.

Tokens are generated with cryptographic randomness, only SHA-256 digests are stored, and expire after 30 minutes. Links place tokens in URL fragments, which are removed immediately by the client; tokens must never appear in server logs or analytics.

## Server configuration (not yet supplied or deployed)

Configure privately (see `.env.example` and `.env.production.example`):

```dotenv
AUTH_PUBLIC_URL=https://YOUR_FINAL_QUIZ_DOMAIN
AUTH_FROM_EMAIL=YOUR_VERIFIED_SENDER
AUTH_SMTP_HOST=YOUR_PROVIDER_SMTP_HOST
AUTH_SMTP_PORT=587
AUTH_SMTP_USERNAME=YOUR_PROVIDER_USERNAME
AUTH_SMTP_PASSWORD=YOUR_PRIVATE_PASSWORD
```

Port 587 uses STARTTLS, port 465 SSL. The sender domain must be configured with SPF/DKIM/DMARC with the mail provider. Never commit actual secrets, send tokens to logs, or use an unverified sender. New email signup/verification intentionally fail closed if email transport is unconfigured. Do not publish the updated front-end without the updated backend and DB schema.

## Safe rollout order

1. Confirm final HTTPS Quiz domain and configure/test sender, SPF/DKIM/DMARC, rate limits/abuse protection.
2. Back up production PostgreSQL and rehearse restore on a separate copy. Record existing Alembic revision and counts of `users`, sessions, runs, appeals, star ledger entries.
3. Test Alembic `0015_email_accounts` on that restored copy; check no existing UUID or FK changes.
4. Configure SMTP and `AUTH_PUBLIC_URL` privately; run the coordinated backend/frontend release; create NO webhook.
5. E2E: create two accounts with identical display names and distinct emails, receive and verify messages; old username login/claim; wrong address correction; expired/reused links; reset revokes sessions; UI locales and RTL; refresh and resume active games.
6. Compare pre/post DB record counts and exact UUID references. Roll back application safely if needed, keeping the additive DB schema until compatibility is established.
7. After legacy players have claimed email, **separately** deprecate old username registration/login and remove old suggestions/unique-username code. Do not drop legacy fields prematurely.
8. Only after independent payment verification design, add Buy Me a Coffee subscription linking. Email equality alone cannot authenticate ownership of a purchase.

## Known release blockers / follow-ups

- Live SMTP provider, secrets, sender reputation and an actual mailbox delivery test are unavailable in this PR.
- API anti-abuse protection still needs a deployment-aware **IP and email rate limiter** beyond the current per-account one-hour cap for verification/reset sends. Respect trusted proxy headers; don't treat the shared Caddy reverse-proxy IP as a user.
- Review accessibility and localization in all supported languages, not only canonical English. Test RU/EN and RTL in browser.
- Add real Postgres concurrency tests for competing email claims and reset requests, as well as a production backup/restore rehearsal.
- Do not merge, deploy to VPS, or accept money for these benefits until all release checks are complete.
