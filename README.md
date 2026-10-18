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

## Learn how it works

- [Local run guide](docs/LOCAL_DEVELOPMENT.md)
- [How the API and frontend communicate](docs/ARCHITECTURE.md)
- [Roadmap and requirements](docs/ROADMAP.md)

## Known limits before production

This is a *local prototype*, not a deployed public service. In-memory sessions, no login, no database, no spending limits/rate limits, no persistent achievements and no paid hint actions yet. The existing AI provider integration has **not** been verified with a real API key in this iteration. Angular's full dependency installation/build must be verified on a connected development machine.

Local test suite does not use an AI API key. The user-facing question language selector does not yet translate the interface; interface localization is planned separately.
