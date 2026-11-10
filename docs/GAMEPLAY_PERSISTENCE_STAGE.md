# DB gameplay persistence — staged rollout

Branch: `feature/quiz-db-gameplay-persistence` (based on `feature/quiz-web-foundation`).
No changes to `master` or Render.

## Stage A — database repository (implemented)

- `backend/persistence/gameplay.py` stores and retrieves authenticated
  player-owned rounds, pending question candidates, answers, progress and star
  milestones.
- `backend/tests/test_gameplay_repository.py` contains 9 independent SQLite
  contract tests. These passed locally on 31 Oct 2026 before route integration.
- Existing Alembic revision `0001_initial` and models are unchanged.
- Repeated answers cannot award stars twice. The DB uniqueness constraint
  adds an independent safeguard.
- Candidate question fingerprints include category/difficulty to preserve
  immutable provenance, with savepoint recovery on concurrent duplicate
  candidate insertions across different rounds.
- No wallet currency or scoring policy has been invented.

## Stage B — versioned authenticated HTTP API (implemented, NOT yet locally tested)

- `backend/persistence/game_routes.py` adds:
  - `POST /api/v2/games` (requires login; 201)
  - `GET /api/v2/games/{run_id}` (requires ownership)
  - `GET /api/v2/games/{run_id}/question` (returns the cached pending
    question if present; otherwise generates + persists one)
  - `POST /api/v2/games/{run_id}/answer` (one atomic answer transition)
- All game endpoints require the existing `quiz_session` HttpOnly cookie.
  POST endpoints reuse the existing origin check.
- The public question contains only the question and A/B/C/D choices; the
  correct key stays server-side until answer feedback is returned.
- A process restart no longer loses v2 games or pending questions.
- Per-round categories are a deterministic shuffle derived from the round
  UUID. This requires no extra migration and does not repeat a category
  within the 16-question game.
- The same bounded AI generator/validator is used for both old and new routes:
  `generate_valid_question()` was extracted from `api.py`; retries,
  local answer-leak filtering, quality-checking, stop reasons and diagnostic
  events are preserved.
- AI generation runs **outside** the PostgreSQL game-row write lock. The
  transaction checks again before insertion, so two simultaneous question
  GETs cannot create two pending question attempts for the same round.
  Concurrent first GETs may both consume an AI request; avoiding duplicate
  AI cost requires an additional generation-claim/lease mechanism.
- `backend/tests/test_game_routes_v2.py` adds **9 offline API tests** for
  authentication, origin protection, ownership, cache/reload, replay, invalid
  language, stats and milestone stars.
- The Angular app still uses **legacy `/api/games`**. That endpoint is
  intentionally unchanged to preserve existing prototype tests while
  moving to the authenticated web UI.
- Live PostgreSQL concurrency testing is still required before the
  legacy endpoints are retired.

## Apply / verify on Windows in PyCharm

At repository root (confirm there are no uncommitted local changes that
conflict with the pull):

```powershell
git switch feature/quiz-db-gameplay-persistence
git pull
cd backend
python -m persistence.seed_topics
python -m unittest discover -s tests -p test_game_routes_v2.py -v
python -m unittest discover -s tests -q
```

Expected if all newly added tests pass: 197 total (188 baseline + 9 new).
This is **not** a claim that they have already passed.

Start backend:

```powershell
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs`. With the existing browser cookie from
`POST /api/auth/login` (or register):
1. `POST /api/v2/games` with `{"language":"English"}` — record `game_id`.
2. `GET /api/v2/games/{game_id}` — should show `phase=main`.
3. `GET /api/v2/games/{game_id}/question` — may invoke the configured real AI
   provider and consume tokens. It returns a question with no answer key.
4. Repeat the GET — should return the *same* question without another AI call.
5. `POST /api/v2/games/{game_id}/answer` with
   `{"question_id":"<uuid from question>","answer":"A"}`.
6. Repeat that same POST — must be rejected with HTTP 409.
7. Check `GET /api/profile/me/stats` — the answered/correct counts should
   reflect the stored attempt.
8. Restart FastAPI, check the same game ID. When a question is pending,
   repeat step 3 to verify that it survives a restart.

Do NOT paste passwords, cookies, or complete `DATABASE_URL` strings into
reports or GitHub comments.

## Next stage

- Review + run all 197 Python tests, including a real PostgreSQL race test
  for concurrent answers and simultaneous question GETs.
- Change Angular to provide register/login/logout UI and switch its game
  requests to `/api/v2/games`.
- Replace the browser's `localStorage` star totals with server-derived
  `/api/profile/me/stats`.
- Retire `/api/games` + process-local session dictionary after migration.
- Define and test round-score and reward-credit policies separately; do not
  imply that 5/10/15 star milestones earn spendable wallet credits.
