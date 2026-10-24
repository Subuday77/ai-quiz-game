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

## Learn how it works

- [Local run guide](docs/LOCAL_DEVELOPMENT.md)
- [How the API and frontend communicate](docs/ARCHITECTURE.md)
- [Roadmap and requirements](docs/ROADMAP.md)

## Known limits before production

This is a *local prototype*, not a deployed public service. In-memory sessions, no login, no database, no spending limits/rate limits, no persistent achievements and no paid hint actions yet. The existing AI provider integration has **not** been verified with a real API key in this iteration. Angular's full dependency installation/build must be verified on a connected development machine.

Local test suite does not use an AI API key. The user-facing question language selector does not yet translate the interface; interface localization is planned separately.
