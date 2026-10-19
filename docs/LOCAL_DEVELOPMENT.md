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
- **Question generation returns HTTP 503:** check backend logs and confirm AI provider, model ID, access, and credentials. The application does not retry provider exceptions; the SDK may already have retried internally (see diagnostics below).
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


## 5. Diagnose a slow question request (Issue #2)

This patch adds diagnostics only. Existing retry counts, timeouts, game rules,
response bodies, and Angular behavior are unchanged. A five-minute successful
request alone does not establish its cause; reproduce it with these logs enabled.

### Capture the browser request

1. Open Chrome or Firefox DevTools (F12), select **Network**, enable **Preserve
   log** / **Persist Logs**, and filter to Fetch/XHR or `/question`. Keep DevTools
   open before requesting the next question.
2. Select `GET /api/games/{game_id}/question`. Check the URL: through Angular it
   should use port 4200; `frontend/proxy.conf.json` forwards `/api` to port 8000.
   Record the start time, method, status, and elapsed time. A pending request has
   no final HTTP status yet. A completed 200 means success; 502 means three invalid
   questions; 503 means a provider/setup exception escaped to the application.
   A proxy-generated failure may have a different body and no diagnostic header.
3. Open **Timing** (Chrome) or **Timings** (Firefox). Inspect queueing/stalled,
   connection, **Waiting for server response (TTFB)** / **Waiting**, and content
   download/receiving. Large browser queueing happens before the request reaches
   FastAPI. Large waiting time includes proxy and backend processing; it does not
   by itself identify a slow AI provider.
4. Once response headers arrive, copy `X-Diagnostic-Request-ID` from **Response
   Headers**. Search the PyCharm backend console for the same `request_id`.
   While pending, use the request start time and the backend's UTC `timestamp` to
   find `question_request_received`, then follow that ID. Convert the browser's
   local time to UTC. The browser sends no correlation header; the server generates
   a fresh ID for every question request. The ID is available in headers only after
   the backend finishes processing. Avoid multiple concurrent reproductions if
   correlating by time while pending.
5. Compare the browser duration with `question_request_completed.duration_ms`.
   If the browser waits a long time before any corresponding received event,
   inspect the Angular terminal for proxy errors and check that the backend is
   reachable. A short backend duration with a long browser duration points to
   delays outside the measured backend processing. Backend logs end when the
   response is ready, before delivery to the browser. Uvicorn's access log normally
   appears at completion, so its absence while pending does not prove non-arrival.

### Read the structured backend events

The `quiz.diagnostics` standard-library logger emits INFO-level JSON lines to the
backend console without additional Uvicorn flags. Every line includes a UTC
`timestamp`, generated `request_id`, and `attempt` (1?3 during generation; otherwise
null). Context variables carry correlation into FastAPI's worker thread without
changing generator or provider public signatures. Durations use `perf_counter()`
and are expressed in milliseconds.

| Event | Meaning |
| --- | --- |
| `question_request_received` | FastAPI middleware received the GET, before worker-thread dispatch. |
| `session_lock_wait_started` / `session_lock_acquired` | The acquired event's `duration_ms` measures waiting for the per-session lock. A wait-start with no acquisition indicates an outstanding lock wait. |
| `question_cache` | `hit=true` returns the current unanswered question without provider setup, generation, or validation; `hit=false` starts generation. |
| `generation_attempt_started` / `generation_attempt_completed` | Each application attempt and its `success`, `invalid_question`, or `provider_failure` outcome. |
| `provider_client_setup_started` / `_completed` / `_failed` | Client construction is measured separately; a fresh client is currently built each attempt. AWS credential discovery may add setup latency. |
| `provider_configuration` | Allowlisted resolved SDK timeouts, retry settings, provider, and SDK versions. No model IDs, credentials, or headers. |
| `provider_request_started` / `_completed` / `_failed` | One SDK call, including internal retries and backoff; completion/failure carries `duration_ms`. |
| `question_json_parse_started` / `_completed` / `_failed` | JSON parsing time, outside the provider call timer. |
| `question_validation_started` / `_completed` / `_failed` | Pydantic validation and difficulty check time, outside the provider call timer. |
| `question_request_completed` | Total backend processing duration, HTTP status, and `success`/`failure`, including handled 404/409/500/502/503 outcomes. Unexpected exceptions also emit `question_request_failed`. |

Large `session_lock_acquired.duration_ms` identifies lock contention. The session
lock remains held throughout question generation, so a second request can wait
behind a slow first request and then return its cached question. Large
`provider_request_completed.duration_ms` (or `_failed`) with a small lock wait
identifies time inside the provider SDK, including network time and SDK retry
sleep. A provider start with no completion/failure identifies an outstanding SDK
call. Repeated application attempt numbers accompanied by parse/validation
failures identify application retries. Large setup time is separate from all of
these. A gap between request receipt and lock-wait start can include worker-thread
scheduling or session lookup, rather than the per-session lock.

No keys, request headers, prompts, question text, answers, language/category values,
or customer data are emitted by these events. Errors log only the exception class,
never its message or traceback: Pydantic and provider exception text can include
input content. Keep SDK HTTP/debug logging disabled when collecting this safe
trace; those independent loggers can expose request/response data.

### Effective SDK timeout and retry behavior

The app supplies no explicit timeout to any provider. It supplies `max_retries=5`
only to NVIDIA, through the same OpenAI Python SDK used for OpenAI.

| Provider | SDK timeouts per attempt | SDK retries | Maximum SDK attempts per application attempt |
| --- | --- | --- | --- |
| OpenAI | Default: connect 5 seconds; read/write/pool 600 seconds | Default `max_retries=2` | 3 |
| NVIDIA | Same OpenAI SDK timeout defaults | `MAX_RETRIES=5` from `consts.py` | 6 (initial call plus five retries) |
| AWS Bedrock | Botocore defaults: connect 60 seconds, read 60 seconds | Inherited AWS configuration; legacy default typically 5 total attempts; standard/adaptive default 3 total | 5 legacy or 3 standard/adaptive unless overridden |

OpenAI retries eligible connection/timeout failures and HTTP 408/409/429/5xx, with
backoff and supported server retry delays. Those retries happen *inside* one
`provider_request_*` span; they do not increment the application's `attempt` field.
The aggregate span cannot distinguish model processing from SDK retry sleep.
This patch intentionally does not turn on raw SDK logging or claim to count
individual wire attempts. See [official OpenAI timeout/retry documentation](https://developers.openai.com/api/docs/guides/flex-processing).

Bedrock inherits `AWS_RETRY_MODE`, `AWS_MAX_ATTEMPTS`, and shared AWS profile
settings. `provider_configuration` records `retry_mode`, `sdk_total_max_attempts`,
and `sdk_max_attempts` from `client.meta.config.retries`. A null attempt setting
means the SDK's modeled default applies; it does not mean zero retries. AWS env/
profile `max_attempts` includes the initial call, whereas a Botocore `Config`
`max_attempts` value counts retries; `total_max_attempts` always includes the
initial call. See [Boto3 retries](https://docs.aws.amazon.com/boto3/latest/guide/retries.html)
and [Botocore timeout settings](https://docs.aws.amazon.com/botocore/latest/reference/config.html).

`api.py` allows up to **three application attempts**, retrying JSON/schema errors
and `ValueError` (including a difficulty mismatch). Consequently, the upper bounds
are **9 OpenAI SDK attempts**, **18 NVIDIA SDK attempts**, and **15 Bedrock attempts
with legacy defaults** (9 with standard/adaptive defaults), or **3 ? the configured
Bedrock total-attempt limit**. Reaching these bounds requires earlier SDK calls
to eventually succeed but return invalid questions, allowing the next application
attempt. An SDK exception escaping on the first application attempt returns 503
immediately: its bound is only 3, 6, or the Bedrock SDK limit, respectively. Invalid
questions on all three application attempts return 502.

These are attempt-count bounds, not guaranteed wall-clock deadlines. Socket
read timeouts are inactivity limits; retries, backoff, client setup, and lock waits
add time. There is no application request deadline or session-lock timeout here.
Thus a successful response after five minutes is possible under current settings.

For reproducibility, the diagnostic tests ran with the repository-root `.venv`:
OpenAI 3.16.2, Boto3/Botocore 1.43.98. Local SDK source inspection confirmed the
OpenAI timeout/retry defaults and Botocore's legacy modeled five-attempt default;
no AI request was sent. Requirements do not pin these SDK versions, so the runtime
`provider_configuration` events on the machine reproducing the stall are the
source of truth. No retry count, timeout, or `.env` value was changed by this patch.

Run offline verification from `backend` with the configured interpreter:

~~~powershell
python -m unittest discover -s tests -v
python -m py_compile api.py question_generator.py define_ai_provider.py
~~~

If the repository-root environment is selected rather than `backend/.venv`, use
`..\.venv\Scripts\python.exe` in place of `python`. Tests mock SDK constructors
and calls, including credential discovery; they require no real AI keys.
