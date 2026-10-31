# Angular account UI and PostgreSQL-backed quiz — local rollout

Work branch: \`feature/quiz-db-gameplay-persistence\`.
Production \`master\` and Render are intentionally unchanged.

## What the frontend now does

- Uses \`GET /api/auth/me\` on page load to restore a server-side cookie session.
- Provides **Sign in**, **Create account**, and **Sign out** using SweetAlert2.
- Sends the username/password **only** to \`/api/auth/login\` or \`/api/auth/register\`
  over relative, same-origin \`/api\` requests. It never puts credentials in
  browser localStorage or logs. The server stores Argon2id password hashes
  and an HttpOnly opaque session cookie.
- Requires an authenticated session before starting a game.
- Calls the authenticated \`/api/v2/games\` routes. These store game progress,
  questions and answers in PostgreSQL; \`/api/games\` remains available for
  legacy callers during the migration.
- Loads the **all-time** bronze/silver/gold/platinum counters from
  \`GET /api/profile/me/stats\`, including a refresh after each answer. No
  browser-local balance or spendable currency is treated as authoritative.
- Only keeps the current *unfinished game ID* in localStorage, under a key
  scoped by the logged-in user ID. A returning player can click **Resume
  game** after the server verifies ownership and current state.
- Shows current-round milestones as the player advances; these are UI
  indicators, not wallet credits. Failed/repeated answers do not award stars.

## Required local environment

In the **untracked root** \`.env\`:

\`\`\`dotenv
DATABASE_URL=postgresql+psycopg://quiz_local:URL_ENCODED_PASSWORD@127.0.0.1:5432/ai_quiz_game
AUTH_COOKIE_SECURE=false
AUTH_ALLOWED_ORIGINS=http://localhost:4200,http://127.0.0.1:4200
\`\`\`

Do not share or commit credentials. \`AUTH_COOKIE_SECURE=false\` is **only**
for local HTTP development; production must use HTTPS/secure cookies.

Use the same browser origin throughout the test (prefer
\`http://127.0.0.1:4200\`). A login made directly in Swagger on port 8000
does not necessarily share its browser cookie with the Angular origin.

## Run and verify

Back end, in **PyCharm** from \`backend\`:

\`\`\`powershell
python -m persistence.seed_topics
python -m unittest discover -s tests -q
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
\`\`\`

Front end, in **VS Code** from \`frontend\`:

\`\`\`powershell
npm ci
npm test
npm run build
npm start
\`\`\`

Open \`http://127.0.0.1:4200\`.

Manual acceptance tests:

1. As a guest, verify that **Sign in** and **Create account** appear and the
   Start button opens login rather than making \`POST /api/v2/games\`.
2. Create an account with a unique username (3–30 ASCII letters/digits/_,
   starts with a letter) and a password (8–256 characters); confirm that
   mismatched passwords are rejected in the form.
3. Sign out, sign in again and confirm that the username and DB-backed
   all-time rewards return.
4. Start a game, inspect Network: \`POST /api/v2/games\` must return 201
   and \`GET .../question\` 200; response must NOT include \`correct_answer\`.
5. Answer a question, verify the profile answered count changes, then
   refresh the Angular page while a later question remains unanswered.
   The **Resume game** action must restore the same current question ID,
   without calling the AI again.
6. Sign out with a pending round, sign in as a different user: the prior
   user's round must not appear. Sign back in as the original account and
   verify **Resume game** works.
7. Verify no \`ai-quiz-game:local-star-totals:v1\` values are written or
   read by the current code. Existing old browser cache keys may remain but
   are ignored by the new frontend.
8. Disconnect PostgreSQL or stop FastAPI and confirm errors are displayed
   without modifying the locally displayed all-time star balance.

The Node tests use mocked HTTP and RxJS virtual timers. A real browser
SweetAlert2/HTTP-cookie test and an Angular build are also required before
merging. This environment cannot run \`npm ci\` without network access, so
verify locally in VS Code and share any failures.

## Limits / planned work

- Current Angular interface is in English, regardless of question language.
  UI localization remains a separate step.
- There is no Google OAuth, password reset or email recovery yet.
- The game score and spendable credit conversion policy are still undecided.
  No wallet credits are minted by game results.
- Public deployment is blocked until abuse limits, production HTTPS and
  recovery flows are ready.
