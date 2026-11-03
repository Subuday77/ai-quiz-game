# AI Quiz Game

A Python + Angular AI-generated trivia game (15 main questions plus a secret 16th).

Development continues in the **feature/quiz-web-foundation** branch. Do not merge it into master until local verification.

## Current local prototype

- Python FastAPI backend, Angular standalone frontend, and an Angular dev-server proxy to /api.
- AI question generation via the existing OpenAI / NVIDIA / Bedrock adapter.
- Pydantic validation: one question, four nonempty distinct choices (A–D), a valid answer key, and the expected difficulty. Invalid AI-generated data is retried at most 3 times.
- Server-side game state and correct-answer verification. The current question response does **not** contain the correct answer. Answer submissions are single-use.
- 15 levels (difficulty 0–14): bronze at 5, silver at 10, gold at 15. A no-hint run unlocks a secret difficulty-15 question with a platinum award on success.
- No persistence yet: stars and games reset when the server restarts. The console version still works.

## Repository layout

~~~text
ai-quiz-game/
├── backend/
│   ├── api.py                    # FastAPI endpoints and temporary sessions
│   ├── question_schema.py        # Pydantic validation + public response
│   ├── game_logic.py             # Independent game rules
│   ├── question_generator.py     # Calls the configured AI provider
│   ├── define_ai_provider.py     # OpenAI / NVIDIA / Bedrock adapter
│   ├── main.py                   # Original terminal game
│   ├── consts.py / config.py
│   ├── requirements.txt
│   └── tests/
├── frontend/                     # Angular application
├── docs/ROADMAP.md
├── docs/LOCAL_DEVELOPMENT.md
└── .env.example
~~~

## Start locally using VS Code (Windows PowerShell)

Prerequisites: Python 3.10+, Node.js 22.12+ or compatible Angular-supported LTS, npm.

**Terminal 1 — Python backend:**

~~~powershell
git switch feature/quiz-web-foundation
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item ..\.env.example .env
~~~

Edit backend/.env: set LLM_PROVIDER, the selected provider's API key and model ID (for example NVIDIA_API_KEY and NVIDIA_MODEL_ID). Never commit secrets.

~~~powershell
python -m unittest discover -s tests -v
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
~~~

Test API: http://127.0.0.1:8000/docs and http://127.0.0.1:8000/api/health

**Terminal 2 — Angular:**

~~~powershell
cd frontend
npm install
npm start
~~~

Open http://127.0.0.1:4200 — Angular requests /api on port 4200 and its proxy forwards to FastAPI at port 8000. You do not need to configure CORS for this local setup.

If PowerShell blocks activation scripts, either configure process-local execution policy or run commands using the .venv Python executable directly.

## Generator prompt versions and A/B test

The single authoritative 0–15 difficulty scale remains in `backend/consts.py`.
Both the unchanged legacy prompt (v1) and the revised level-calibrated prompt
(v2) now live in `backend/agents/generator.py`. The production generator
(`backend/question_generator.py`) selects one via
`AI_GENERATOR_PROMPT_VERSION=v1|v2` (defaults to **v1** while v2 is tested).
The generator's provider, model, reasoning effort, temperature, JSON contract,
validation, and retry policy are unchanged. Critic v3 remains audit-only.

From the **project root** in the Python environment (PyCharm terminal or
PowerShell), compare the two prompts with eight blind question pairs:

~~~powershell
python -m unittest discover -s tests -v
python test_openai_generator_ab.py
~~~

Use `python test_openai_generator_ab.py --all` for ALL **15 main game
levels 0..14** (15 blind pairs, 30 billable generator API calls), or
`--pairs 2` for a quick smoke test. Level 15 is the separate bonus round,
not part of the 15-question main game. To check whether one odd wording
choice is a sampling accident, independently repeat its level/category with
`python test_openai_generator_ab.py --level 1 --repeats 3` (3 extra
blind pairs, 6 calls). Both prompts see the same language, category and
requested difficulty in each pair; the model may choose different facts
between pairs. One sample at a given level cannot establish whether a
wording defect is systematic. `--repeats` can be combined with `--all`,
but multiplies the API cost. No critic calls or scoring-based rejections
are used. Results are written to
`generator_ab_<timestamp>.md` (blind side-by-side questions with answers and
version labels at the bottom), plus CSV/JSONL. Assess factual correctness,
distractor strength, fairness and interest manually; valid JSON alone is not a
quality win.

After review, explicitly enable v2 for a local game with
`AI_GENERATOR_PROMPT_VERSION=v2` in the project's active `.env`, then restart
the backend. No changes to master are required or implied.

## Fact-quality hardening (24 Oct 2026)

The reviewer-independent **literal answer-leak gate** is described in
[Answer validity experiment](docs/ANSWER_VALIDITY_EXPERIMENT.md). It runs
before the optional critic even when `AI_QUALITY_MODE=off/audit`. Its default
is `AI_LOCAL_LEAK_MODE=enforce`, which rejects only high-confidence literal
answer-in-stem shortcuts and uses the existing bounded regeneration budget.
Set `AI_LOCAL_LEAK_MODE=audit` to observe without blocking if regression
testing reveals false positives. Critic v3 stays audit-only.

The **key-blind four-option factual checker** is currently a separate
experiment, NOT a live publication rule:

~~~powershell
python -m unittest discover -s tests -v
cd backend
python -m unittest discover -s tests -v
cd ..
python test_openai_answer_check.py --limit 4
python test_openai_answer_check.py
~~~

Root tests and backend tests are different suites. The live check sends
14 frozen examples to the configured OpenAI reviewer model (billable).
The model receives no supplied answer key or human labels. Inspect JSONL
results for false positives and missed multiple/no-correct-answer cases
before allowing AI-based rejection. Persistent storage, player reports,
tags and game telemetry will be implemented with the PostgreSQL phase.

## Bonus quality & Terra arbitration (3 Nov 2026)

**This feature is implemented on `feature/quiz-fact-dedup-tags`, not yet merged
into `main`.** It requires the fourth Alembic migration for bonus topic
recovery; run from `backend/` BEFORE restarting the server:

```powershell
git pull origin feature/quiz-fact-dedup-tags
python -m alembic upgrade head
python -m unittest discover -s tests -v
```

The model configuration is **not** part of source control. Configure these in
the existing project-root `.env` before running the backend:

```dotenv
AI_TAG_DEDUP_ENABLED=true
AI_QUALITY_MODE=enforce
AI_GENERATION_DEBUG=true
QUIZ_QUALITY_ARBITER_MODE=on
QUIZ_QUALITY_ARBITER_MODEL_ID=gpt-6.1-terra
QUIZ_QUALITY_ARBITER_TIMEOUT_SECONDS=10
```

The existing `OPENAI_API_KEY` is used; no second API key is needed. **Terra
requests are billed separately.** Model availability must be confirmed on the
actual OpenAI account; offline tests mock the provider and do not establish
real API access. In active mode, Terra independently reviews **every bonus
candidate** and any normal candidate that triggers a shadow `would_arbitrate`
signal. It may veto but never override a failing primary critic.
The strict output captures separate A/B/C/D judgments and an array of
independent assertions from the FULL stem (including secondary river names,
years, and claims not represented in the fact triple).

**This is LLM arbitration, not independent web/source verification.** The
arbiter has no search access. If it cannot substantiate an assertion, its
contract explicitly requires `uncertain` and the question is not accepted.
Arbitration failures also fail closed for the candidate. Routine INFO logs
contain model ID, decision, elapsed duration and reason; the **full verdict,
claims and options** are present only in the existing opt-in `DEBUG:`
`quality_arbitration_debug` event. DEBUG logs include candidate content and
correct answers: never expose these logs to public clients.

Previously approved questions identified by the 3-Nov browser QA report
(calvados ambiguity, Savarin surname leak, Roman/Rome name leak, alleged
Hua Rong Dao derivative, misnamed Puerto Williams airport, unsupported Thimphu
river) are excluded from **shared-pool reuse by exact stem + keyed-answer
matching**. This is a conservative safety hold, not a database deletion or
an automatic factual claim about other questions. Original game history and
awards remain intact. A later user-report workflow can adjudicate these records.

At difficulty **15 (bonus)** a primary-critic `too_easy` verdict rejects the
question regardless of its total weighted score. The `too_easy_only` emergency
reserve is disabled for the bonus. Approved global-pool fallback still requires
matching language, difficulty, category, a previously accepted v5 review, and
user-specific fact dedup. If no valid question is available, the game preserves
the 15/15 progress and offers **Retry**.

After an unsuccessful bonus generation (after any configured approved-pool
fallback is exhausted), the **server automatically rotates** to the next
category in a stable per-game permutation. The new selection is persisted on
`GameRun`, so the next ordinary `GET /question`—including a normal Retry
after HTTP 502/503—uses the new topic. Every failed attempt advances at most
one topic; after all categories have been visited, the sequence wraps and
starts again, with no lifetime limit. No extra AI call is issued immediately
by the rotation itself, respecting the existing per-request time and cost
budget. Bonus eligibility, 15/15 progress, gold and all previous answers
remain intact. A previously issued Q16 cannot be replaced; a concurrent stale
generation for the former topic is discarded at the commit-side row lock.

**Topic selection is internal, not a player action.** The former
`POST /api/v2/games/{id}/bonus/change-topic` endpoint and Angular button
have been removed. The persisted index and original topic-rotation mechanism
are retained to support future explicit topic-selection *star perks*, which
will require separate authorization/charging design. For now the UI presents
only Retry after failure and never both Retry and Next question.


Run offline tests from `backend/` using
`python -m unittest discover -s tests -v`; Angular in `frontend/`:
`npm test` and `npm run build`. GitHub Actions now includes both suites
and an Angular build. A browser/PgSQL smoke run is still needed before merging.

## P1: bounded tagged metadata recovery (2 Nov 2026)

The tagged generator uses provider-native strict JSON Schema plus a stricter
Pydantic contract. The portable provider schema does not constrain array
lengths, while `TagCloud.entity` allows **1-3** items. In a real browser run
the generator repeatedly returned four entity tags and exhausted three
otherwise valid candidate attempts.

The server now performs an extremely narrow, deterministic metadata repair:
if the **only Pydantic field error** is four entity tags, all four are unique
valid canonical slugs, and the unchanged `fact.subject` is present, keep three
entities (always retaining the fact subject). Then validate the **whole**
question again. No question text, choices, answer key, category, difficulty
or fact triple is modified. Five or more entities, bad slugs, missing
fact subjects, or any other failing validation are still rejected.

For unrepaired invalid tagged responses, the next generation gets a fixed,
server-authored instruction about the required schema; previous raw provider
text, Pydantic error content and private user history are never inserted into
this feedback. If the generator returns an identical invalid JSON object on
two attempts, a request-local hash terminates the redundant third call;
the existing approved-question pool / strict reserve fallback then runs.

INFO diagnostics: `tagged_metadata_repaired`,
`tagged_validation_retry_feedback`, `generation_repeated_invalid_stopped`,
and the usual `generation_retries_exhausted`. Legacy untagged generation
remains unchanged. No extra LLM model or paid validation call is introduced.

Offline regression:
`cd backend; python -m unittest discover -s tests -p "test_tagged_metadata_recovery.py" -v`.

## PostgreSQL accounts + question history foundation (24 Oct 2026)

The dev branch now contains a versioned PostgreSQL schema
(`backend/migrations/versions/0001_initial.py`) for username/password
accounts, server-side sessions, topic/tag clouds, scores, stars, achievements,
question attempts, timing, quality reviews and player dispute records.

New endpoints: `POST /api/auth/register`, `POST /api/auth/login`,
`GET /api/auth/me`, `POST /api/auth/logout` and
`GET /api/profile/me/stats`. Passwords use Argon2id; opaque
session tokens are stored as hashes and sent using HttpOnly cookies.
Google sign-in and a SweetAlert2-based Angular form will come later;
**SweetAlert2 is UI only**, authentication is on FastAPI.

The existing local in-memory `/api/games` flow is **not yet persisted** and
not protected by account auth. Do not deploy it publicly in this state.
See **[PostgreSQL + Accounts setup](docs/POSTGRESQL_ACCOUNTS.md)**
for schema explanation, `.env` settings, PostgreSQL migration and tests.
Account endpoints require a configured `DATABASE_URL` and migrated database;
there is no implicit SQLite runtime fallback.

## Learn how it works

- [Local run guide](docs/LOCAL_DEVELOPMENT.md)
- [How the API and frontend communicate](docs/ARCHITECTURE.md)
- [Roadmap and requirements](docs/ROADMAP.md)

## Known limits before production

This is a *local prototype*, not a deployed public service. In-memory sessions, no login, no database, no spending limits/rate limits, no persistent achievements and no paid hint actions yet. The existing AI provider integration has **not** been verified with a real API key in this iteration. Angular's full dependency installation/build must be verified on a connected development machine.

Local test suite does not use an AI API key. The user-facing question language selector does not yet translate the interface; interface localization is planned separately.
