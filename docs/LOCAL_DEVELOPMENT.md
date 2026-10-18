# Local development (Windows: PyCharm backend + VS Code Angular)

The project contains two independent development processes:
- **Backend:** Python + FastAPI, developed and run in **PyCharm**.
- **Frontend:** Angular, developed and run in **VS Code**.

Do not run Python from VS Code. No Docker or Render deployment is needed for this local stage. Open the same repository folder in both IDEs, but work with their respective parts.

## 0. Confirm the Git branch

In PyCharm's terminal (or a separate PowerShell), from the repository root:

~~~powershell
git branch --show-current
git status
~~~

The current branch should be `feature/quiz-web-foundation`. A plain `git pull` only updates the branch that was already checked out. If necessary, and **if you have no uncommitted changes to protect**, run:

~~~powershell
git fetch origin
git switch feature/quiz-web-foundation
git pull
~~~

Keep `master` unchanged for now.

## 1. Backend in PyCharm

1. Open the repository (or its `backend` directory) in **PyCharm**.
2. Configure the Python project interpreter to use `backend/.venv/Scripts/python.exe`. If the virtual environment does not exist yet, create one in PyCharm (Settings > Project > Python Interpreter > Add Interpreter > Local Interpreter) with the environment located at `backend/.venv`.
3. In **PyCharm's terminal**, from the `backend` directory, install dependencies using the selected interpreter:

~~~powershell
python -m pip install -r requirements.txt
~~~

4. In `backend`, create a local `.env` file using `../.env.example` as the starting point. If it does not exist yet:

~~~powershell
Copy-Item ..\.env.example .env
~~~

Choose one AI provider and configure its model ID and API key. Example:

~~~dotenv
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=put-your-real-key-locally
NVIDIA_MODEL_ID=your-active-model-id
~~~

The values above are examples only. Never commit your API key, paste it into issues, or send it to the browser.

5. Create a **PyCharm Run/Debug Configuration** with:

- **Type:** Python
- **Run:** Module name `uvicorn` (rather than a script path)
- **Parameters:** `api:app --reload --host 127.0.0.1 --port 8000`
- **Working directory:** the absolute path to `ai-quiz-game/backend`
- **Python interpreter:** `backend/.venv/Scripts/python.exe`

Start that configuration using PyCharm's Run button. Keep the process running while testing Angular.

6. Check the backend at http://127.0.0.1:8000/api/health (expected response: `{"status":"ok"}`) and inspect FastAPI Swagger at http://127.0.0.1:8000/docs.

**Backend tests:** in PyCharm's terminal, with the working directory set to `backend`:

~~~powershell
python -m unittest discover -s tests -v
~~~

**Alternative:** if you prefer a terminal-only launch inside PyCharm, from `backend`:

~~~powershell
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
~~~

To run the older CLI game instead, stop Uvicorn and execute `main.py` in PyCharm.

## 2. Angular in VS Code

Open the repository's `frontend` directory in VS Code and run in its terminal:

~~~powershell
npm install
npm start
~~~

Open http://127.0.0.1:4200. The Angular dev server proxies requests starting with `/api` to `http://127.0.0.1:8000` as configured in `frontend/proxy.conf.json`.

During local development, do **not** open port 8000 to view the Angular UI: port 8000 serves the API, and port 4200 serves the Angular application. A production static-file build is a separate step.

## 3. Troubleshooting

- **`ModuleNotFoundError` on backend launch:** confirm the PyCharm working directory is `backend` and the selected interpreter has packages from `backend/requirements.txt` installed.
- **`python` points to a different interpreter in PyCharm's terminal:** use `.\.venv\Scripts\python.exe` explicitly or activate the project's interpreter; the Run Configuration interpreter is configured separately.
- **Question generation returns HTTP 503:** check backend logs and confirm AI provider, model ID, access, and credentials. Provider/authentication errors are not automatically retried here.
- **Question generation returns HTTP 502:** the provider returned an invalid question after three attempts. Check backend logs.
- **Angular displays a connection error:** verify the PyCharm FastAPI configuration is running on port 8000, Angular runs on 4200, and requests pass through the dev server's `/api` proxy.
- **Game sessions disappear after restart:** expected at this prototype stage; storage is in memory.
- **Dependency installation fails:** initial pip and npm installations require network access.

## 4. Test scope and next learning steps

Python tests exercise the progression and the FastAPI endpoints with a mocked generator. They do not require real AI credentials. The real provider request and Angular-to-backend browser flow still need to be checked locally.

1. Study the request/response flow in `docs/ARCHITECTURE.md`.
2. Refine JSON Schema/structured-output handling, stop reasons, and provider error classification.
3. Add bounded backoff to transient AI failures without retrying invalid credentials.
4. Add AI usage instrumentation, limits, persistent database sessions, and secure login.
