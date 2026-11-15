# Email-first accounts — migration contract (draft)

Status: **implementation plan**, not enabled in production.
Scope: AI Quiz Game accounts. Do not deploy schema/model changes separately from their migrations.

## Current state verified in code (2026-11-16)

- `users.id` already is a UUID and is the stable FK used by the game, star/economy and appeal data.
- `users.username_key` has a UNIQUE constraint; registration rejects duplicates and offers suffix suggestions.
- `auth_identities` uses `provider='password'` and the normalized username as `provider_subject`.
- Passwords already use Argon2id; `auth_sessions` stores digests of opaque cookie tokens.
- Therefore **do not regenerate user UUIDs or migrate game FKs**. Preserve all user IDs and existing history.

## Target model (approved product requirements)

`users`:
- `id` UUID PK **unchanged**.
- `display_name` TEXT/VARCHAR(80), NOT UNIQUE, required for newly created accounts; may repeat across any number of players; Unicode supported.
- `email` (or separate `user_emails` table) canonical address, **unique case-insensitively** and verified before use for account recovery or external purchase claims.
- `email_verified_at` UTC nullable for legacy accounts until verified.
- `username` and `username_key` remain as **legacy, internal migration fields** until the old login path is retired; do not rename or reuse them as public display names.
- `preferred_language`, `ui_language`, `theme_key`, `created_at`, `disabled_at` unchanged.

`auth_identities`:
- Preserve existing Argon2id `password_hash`; do not re-encrypt passwords or derive them from display names.
- During migration, preserve existing `provider_subject` legacy usernames to allow fallback login.
- New email login resolves a verified email to `users.id`, then verifies the password identity **by user_id**.
- Never use display_name to authenticate.

`email_verification_tokens` and `password_reset_tokens` (or a single scoped token table):
- `id` UUID, `user_id` UUID FK, `purpose` ('verify_email' | 'reset_password' | 'change_email'), `token_hash` unique, `expires_at`, `used_at`, `created_at`.
- Store SHA-256 digests of cryptographically random >=256-bit tokens, **never plaintext**. One-time and expiring; rate limited.
- Store email target digest / explicit target email for verification if email-change flows are introduced, so old links cannot verify a newly changed address.

## Rollout in phases

1. **Expand DB**: add nullable email columns and token tables; preserve all current constraints and auth paths. Back up DB, rehearse on a restored copy before production.
2. **Email claim for legacy users**: authenticated old-session user enters email, receives verification link, confirms it, and the address becomes uniquely bound to the same UUID. Prevent account takeover with verified email; never automatically merge users.
3. **New registration and login**: require email, display name, password. No uniqueness checks on display_name. Send verification. Require verified email for password reset and BMAC linking; choose an explicit policy for access to game while verification pending.
4. **Password recovery**: public request endpoint always returns the same response regardless of email existence. On a verified address, send short-lived single-use reset link. Consume token atomically, change Argon2id hash, revoke all existing sessions and reset tokens. Rate limit by IP and target hash, avoid leaking tokens in logs/referrers.
5. **Deprecate old login**: only after all relevant legacy users have a verified email or a safe account-recovery process. Drop `users.username_key` uniqueness **only after all code paths stop relying on it**. Remove suffix suggestions from UI; username/display name may repeat.
6. **Premium**: verified game email can suggest a candidate BMAC match but cannot by itself prove purchase ownership. Bind authenticated user UUID to provider subscription ID after verified claim/checkout. No webhook or payment logic in this migration.

## Proposed HTTP contracts

- `POST /api/auth/register` {email, display_name, password}
- `POST /api/auth/login` {email, password}
- `POST /api/auth/email/request-verification` (authenticated)
- `POST /api/auth/email/verify` {token}
- `POST /api/auth/password/forgot` {email} (always same outward response)
- `POST /api/auth/password/reset` {token, new_password}
- `PATCH /api/auth/display-name` {display_name} (authenticated)
- `GET /api/auth/me` includes email_verified and display_name, but never token/hash.

Exact routes can be adapted to existing frontend endpoints as long as backward compatibility is tested.

## Database and security invariants

- Every existing `users.id` survives unchanged. Existing `game_runs`, `auth_sessions`, star ledger, appeals, question votes, helper records and localization remain associated with that UUID.
- Canonical email must enforce unique identity in DB. Conservative normalization: trim surrounding whitespace, Unicode domain to IDNA, casefold domain; case-insensitive mailbox handling as product policy, **no Gmail-specific dot or plus folding**.
- Display names may duplicate exactly, including different users having the same spelling and case. Names must never be authorization keys.
- Existing verified email cannot be silently reassigned; registration and claim flows handle concurrent duplicate claims with DB uniqueness.
- Never email existing passwords; never give tokens through query logs, analytics, exception traces or API response.
- Password reset invalidates authenticated sessions and is transactional and single-use.
- CSRF/origin checks apply to cookie-authenticated mutations.
- Fail closed if email transport is not configured; no fake success that claims email was sent.

## Acceptance tests before production

- Create two distinct UUID users with exactly identical display_name and distinct emails, both login successfully.
- Same email with case variation rejected by database uniqueness; racing registrations cannot bypass constraint.
- Two pre-migration accounts with same display name retain separate star balances, history, appeals, active rounds and sessions.
- Legacy sign-in and email claim work during migration.
- Verification link: valid, expired, reused and tampered; verify wrong-target rejection.
- Password reset: existent/nonexistent email get same public response; expired/reused tokens rejected; concurrent redemption single-use; previous sessions revoked.
- Email account change and Premium purchase cannot be claimed solely by matching customer-provided email.
- Functional frontend E2E: signup, sign-in, sign-out, password recovery, account rename, refresh/resume, duplicate display names, RU/EN and RTL.
- PostgreSQL backup/restore rehearsal + rollback strategy; only deploy after full test suite.

## Decisions made

- Email becomes the unique login identifier; display names are nonunique.
- Preserve existing UUIDs and Argon2id credential system.
- Do not create Buy Me a Coffee webhook until accounts are ready.

## Unresolved engineering implementation details

- Mail transport (SMTP/API), sender domain, deliverability (SPF/DKIM/DMARC).
- Account access policy before verification.
- Final retention/deletion window for old username login.
- Exact migration revision and sequencing after inspecting current Alembic head and production DB.
