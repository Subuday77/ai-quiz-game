# Local development (Windows / VS Code)

Keep the two processes in **two VS Code terminals**. Both commands are executed from their own directories, not from the repository root.

## Backend — terminal 1

~~~powershell
git switch feature/quiz-web-foundation
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item ..\.env.example .env
~~~

Open backend/.env in VS Code. Choose one provider and set its model ID and API key, for example:

~~~dotenv
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=put-your-real-key-locally
NVIDIA_MODEL_ID=your-active-model-id
~~~

The values above are examples only. The token must not be committed, copied into issues, or sent to the browser.

~~~powershell
python -m unittest discover -s tests -v
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
~~~

Open http://127.0.0.1:8000/docs for FastAPI's interactive Swagger UI.
Try http://127.0.0.1:8000/api/health. Expected response: {"status":"ok"}.

To play the older command-line game instead, stop Uvicorn and run python main.py from backend.

## Angular — terminal 2

~~~powershell
cd frontend
npm install
npm start
~~~

Open http://127.0.0.1:4200. The browser talks to the Angular development server, which forwards /api calls to localhost:8000 using frontend/proxy.conf.json.

Do not use localhost:8000 to view Angular: that server only hosts the API during local development. A production static-file build is a separate step.

## Troubleshooting

- **Python command not found**: make sure the Python launcher is installed. Use python instead of py if needed.
- **Activation blocked**: in this one terminal run Set-ExecutionPolicy -Scope Process Bypass, then retry activation. Alternatively use .\.venv\Scripts\python.exe for every Python command.
- **Question generation fails with 503**: inspect Uvicorn logs and verify the provider, model ID, access and credentials. Authentication failures are not automatically retried.
- **Question generation fails with 502**: the provider answered but did not produce a valid question after three attempts. Check backend logs.
- **Angular shows a connection failure**: ensure FastAPI is running on port 8000 and Angular on 4200. The proxy only works through the Angular dev server.
- **Your game disappears after restart**: expected for the prototype. Persistent storage comes in roadmap stage 3.
- **Can't install packages**: npm and pip require network access for initial setup.

## Test scope

Python tests exercise progression and FastAPI with a mocked question generator — no AI keys or paid requests. Browser and real-model integration must still be checked on a developer machine.

## Next learning topics

1. Understand the request/response flow in ARCHITECTURE.md.
2. Replace the provisional JSON generation path with provider-specific structured output, stop reasons and error taxonomy.
3. Add bounded backoff for transient provider failures while avoiding retrying invalid API credentials.
4. Add AI usage instrumentation, quotas, database sessions and secure login.
