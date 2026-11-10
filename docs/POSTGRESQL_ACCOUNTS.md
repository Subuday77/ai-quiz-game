# AI Quiz Game — PostgreSQL v1 + Accounts (24 Oct 2026)

Implemented in `feature/quiz-web-foundation`. **No Render/master changes.**
This is a standalone, reviewable first migration and server-side account API.
The legacy `/api/games` endpoints still keep game state **in memory** and
are **not yet associated with authenticated users**. This is intentional
until database persistence, permissions and reward transactions are tested.

## Accounts and authentication

- Unique username (case insensitive): 3–30 ASCII letters/digits/underscore,
  starting with a letter. Example `QuizPlayer_27`.
- Password: 8–256 characters; only an **Argon2id hash** is stored.
  No plaintext passwords, reversible encryption, or username-based key.
- The `users` profile stores username, optional display name, language,
  UI theme, status and registration date.
- `auth_identities` holds a password identity now, and will later accommodate
  Google provider-subject IDs linked to **the same user**.
- `auth_sessions` stores only the SHA-256 digest of randomly generated
  opaque session tokens, expiry (7 days) and revocation timestamps.
- Browser cookie: `HttpOnly`, `SameSite=Strict`, `Secure` in production,
  or explicitly local HTTP-only override via `AUTH_COOKIE_SECURE=false`.
- Mutating account endpoints reject unexpected cross-site origins. When
  developing via Angular proxy (`changeOrigin=true`), add the exact
  trusted browser address to `AUTH_ALLOWED_ORIGINS`.
- Five consecutive failed password checks impose a temporary per-account
  lockout. This does **not** replace IP/global rate limits, password-reset
  policy, HTTPS deployment hardening and abuse prevention before production.

Account API, available after DB configuration and migration:

| Method | Endpoint | Meaning |
|---|---|---|
| POST | `/api/auth/register` | Register; set session cookie; return public profile |
| POST | `/api/auth/login` | Verify password; issue opaque session cookie |
| GET | `/api/auth/me` | Return authenticated profile; no password fields |
| POST | `/api/auth/logout` | Revoke browser session |
| GET | `/api/profile/me/stats` | Current user's aggregated scores, stars and topic statistics |

Server checks credentials; **SweetAlert2 is only the future Angular modal UI**
for login/registration (field validation, presentation, errors), not the
security mechanism. Google login is planned but **not yet implemented**.
Without an email or other recovery factor, password recovery must be designed
before wider deployment; do not use security-question-based recovery.

## Data schema

Initial Alembic revision: `0001_initial`. The revision uses a **frozen
independent schema snapshot** at
`backend/migrations/snapshots/schema_v1.py`, so edits to ongoing app models
cannot silently rewrite the first migration. Add subsequent changes through
**new revisions**, never by altering an applied snapshot.

| Table(s) | Contents |
|---|---|
| `users`, `auth_identities`, `auth_sessions` | Profile, auth method, hashed password/session |
| `topics`, `tags`, `question_tags` | Topic catalog, multilingual semantic tags, weighted question-tag links |
| `questions` | Immutable candidate: language, prompt/model versions, question/choices/key, requested difficulty and status |
| `game_runs` | One user's round; score, correct progress, hints, times, status |
| `question_attempts` | Per-question delivery, selected answer, correctness, timing, hint count and difficulty snapshot |
| `star_awards` | Bronze/silver/gold/platinum per round; unique round/tier, no duplicate medals |
| `wallet_transactions` | Separate spendable credit journal, idempotency key, signed transactions |
| `achievement_definitions`, `user_achievements` | Editable achievement rule catalog and earned badges |
| `question_reviews`, `question_reports` | Versioned AI checks, player disputes, adjudication status |

**Semantic tags do NOT include difficulty.** A tag describes a topic,
entity or keyword, may have localized names and weighted relation to a
question. The topic is also a stable foreign key. Requested difficulty
(0..15) is metadata on each question and answer attempt, never part of
tag identity. Real-world response accuracy/time will eventually help
calibrate empirical difficulty without relabeling topic tags.

Percentage answered correctly by topic and percentage share of all questions,
average requested difficulty, average recorded answer time, lifetime score,
best round score, star counts and spendable credits are **derived from
recorded attempts and transactions**, not editable user counters.
The initial statistics endpoint correctly returns zero until the legacy
game loop is migrated to persistence.

Question report reason codes: `incorrect_key`, `multiple_correct`,
`answer_leak`, `ambiguous`, `bad_translation`,
`difficulty_mismatch`, `other`.
Reports are claims, **not confirmed fact labels**. A real report submission
endpoint, review/admin UI, rewards and quarantining workflow are subsequent
increments. The schema preserves room for them now.

Score calculation, leaderboard rules, exchange between different kinds
of stars and spendable credits, and achievement definitions need gameplay
decisions before crediting any real player. No invented award policy is
applied at registration.

## Local PostgreSQL setup — Windows / PyCharm terminal

Prerequisites: PostgreSQL server, database + user created by its admin.
Example SQL to run as PostgreSQL admin (choose your own strong secret):

~~~sql
CREATE ROLE quiz_local LOGIN PASSWORD 'REPLACE_WITH_LOCAL_SECRET';
CREATE DATABASE ai_quiz_game OWNER quiz_local;
~~~

At project root, install new requirements in the Python environment:

~~~powershell
python -m pip install -r backend/requirements.txt
~~~

Add these **only to your untracked root `.env`**:

~~~dotenv
DATABASE_URL=postgresql+psycopg://quiz_local:REPLACE_WITH_URL_ENCODED_PASSWORD@127.0.0.1:5432/ai_quiz_game
AUTH_COOKIE_SECURE=false
AUTH_ALLOWED_ORIGINS=http://localhost:4200,http://127.0.0.1:4200
~~~

If credentials include URL reserved characters, percent-encode the
password. In production use TLS, `AUTH_COOKIE_SECURE=true`,
explicit HTTPS origins, and secret management.

Migrate and seed the existing, pre-defined question categories:

~~~powershell
cd backend
python -m alembic -c alembic.ini upgrade head
python -m persistence.seed_topics
python -m unittest discover -s tests -q
cd ..
python -m unittest discover -s tests -q
~~~

Check `GET http://127.0.0.1:8000/docs` after starting FastAPI.
Creating an account is server-backed; the **existing unprotected demo game
still runs as before**, so do not expose the full app publicly yet.

The offline persistence/auth tests use SQLite **only as a unit-testing
substitute**, not as a production or application fallback. Real PostgreSQL
migration/application verification is still required. No passwords or
DB access credentials have been inserted into committed files.

## Next increments — deliberate separation

1. Validate this migration and the account endpoints against a real local
   PostgreSQL database.
2. Replace in-memory game state with DB-backed runs and attempts in one
   transaction, associate only with authenticated users, preserve existing
   generator retry/budget and answer security.
3. Add report-question submissions, immutable question review metadata,
   moderation/quarantine and anti-abuse controls.
4. Add a multilingual semantic tagger + deduplication and user-topic
   analytics, then achievement rules, star transactions and leaderboard.
5. Build login/register SweetAlert2 Angular modal using these backend APIs;
   later connect Google OAuth identities.
