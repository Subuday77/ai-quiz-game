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
- **Question generation returns HTTP 503:** check backend logs and confirm AI provider, model ID, access, and credentials. SDK retries are disabled. Transient failures use the bounded application policy below; permanent failures are not retried.
- **Question generation returns HTTP 502:** the provider returned invalid questions through the configured attempt limit. Check backend logs.
- **Angular displays a connection error:** verify the PyCharm FastAPI configuration is running on port 8000, Angular runs on 4200, and requests pass through the dev server's `/api` proxy.
- **Game sessions disappear after restart:** expected at this prototype stage; storage is in memory.
- **Dependency installation fails:** initial pip and npm installations require network access.

## 4. Test scope and next learning steps

Python tests exercise progression, FastAPI endpoints, diagnostics, and retry handling with mocked generators/provider SDKs. They do not require real AI credentials. The real provider request and Angular-to-backend browser flow still need to be checked locally.

1. Study the request/response flow in `docs/ARCHITECTURE.md`.
2. Refine JSON Schema/structured-output handling, stop reasons, and provider error classification.
3. Tune the generation budget and retry settings using measured provider latency.
4. Add AI usage instrumentation, limits, persistent database sessions, and secure login.


## 5. Diagnose a slow question request (Issue #2)

The diagnostics from phase 1 remain available. Phase 2 adds the interactive timeout
and retry policy below. Game rules and existing response bodies remain unchanged.

### Capture the browser request

1. Open Chrome or Firefox DevTools (F12), select **Network**, enable **Preserve
   log** / **Persist Logs**, and filter to Fetch/XHR or `/question`. Keep DevTools
   open before requesting the next question.
2. Select `GET /api/games/{game_id}/question`. Check the URL: through Angular it
   should use port 4200; `frontend/proxy.conf.json` forwards `/api` to port 8000.
   Record the start time, method, status, and elapsed time. A pending request has
   no final HTTP status yet. A completed 200 means success; 502 means three invalid
   questions; 503 means the provider is unavailable, retries/budget were exhausted, or configuration failed.
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
| `provider_request_started` / `_completed` / `_failed` | One SDK call; SDK retries are now disabled. Completion/failure carries `duration_ms`. |
| `question_json_parse_started` / `_completed` / `_failed` | JSON parsing time, outside the provider call timer. |
| `question_validation_started` / `_completed` / `_failed` | Pydantic validation and difficulty check time, outside the provider call timer. |
| `question_request_completed` | Total backend processing duration, HTTP status, and `success`/`failure`, including handled 404/409/500/502/503 outcomes. Unexpected exceptions also emit `question_request_failed`. |

Large `session_lock_acquired.duration_ms` identifies lock contention. The session
lock remains held throughout question generation, so a second request can wait
behind a slow first request and then return its cached question. Large
`provider_request_completed.duration_ms` (or `_failed`) with a small lock wait
identifies time inside the provider SDK, including network time. Application retry
sleep is logged separately in `generation_retry_scheduled`. A provider start with no completion/failure identifies an outstanding SDK
call. Repeated application attempt numbers accompanied by parse/validation
failures identify application retries. Large setup time is separate from all of
these. A gap between request receipt and lock-wait start can include worker-thread
scheduling or session lookup, rather than the per-session lock.

No keys, request headers, prompts, question text, answers, language/category values,
or customer data are emitted by these events. Errors log only the exception class,
never its message or traceback: Pydantic and provider exception text can include
input content. Keep SDK HTTP/debug logging disabled when collecting this safe
trace; those independent loggers can expose request/response data.

### Phase 2: interactive timeout and retry policy

Phase 1's 11 real NVIDIA requests succeeded, usually in 2-3.5 seconds and once in
about 8 seconds. Lock contention and validation were negligible. The previous
600-second SDK read timeout and five NVIDIA retries could nevertheless produce
very long waits. With three application attempts, the previous theoretical
attempt ceilings were 9 for OpenAI, 18 for NVIDIA, and typically 15 for Bedrock
legacy mode. These settings have now been replaced by one application retry loop.

Set the following environment variables in your local backend environment (the
same defaults are in `.env.example`). Restart FastAPI and reload the browser after
changing them. No secret `.env` file is modified by this patch.

| Variable | Default | Effect |
| --- | --- | --- |
| `AI_PROVIDER_TIMEOUT_SECONDS` | `12` | OpenAI/NVIDIA connect, read, write and pool socket limits; Bedrock read limit. Bedrock connect is at most 3 seconds. Each attempt's value is reduced to the remaining budget. |
| `AI_GENERATION_MAX_ATTEMPTS` | `3` | Total application attempts, including the first, shared by transient provider failures and invalid JSON/schema/difficulty results. |
| `AI_GENERATION_BUDGET_SECONDS` | `30` | Soft generation budget, starting after session-lock acquisition. No further attempt or retry sleep starts when its budget check fails. |
| `AI_RETRY_BACKOFF_BASE_SECONDS` | `0.5` | First exponential backoff ceiling. Jitter selects uniformly between half the ceiling and the ceiling. |
| `AI_RETRY_BACKOFF_MAX_SECONDS` | `2` | Maximum backoff and maximum accepted server-directed retry delay. |
| `QUIZ_LONG_WAIT_WARNING_SECONDS` | `8` | When the browser displays the long-wait warning. |
| `QUIZ_HTTP_TIMEOUT_SECONDS` | `45` | Finite browser timeout for question, start-game and answer HTTP requests. |

Settings must be finite positive numbers; attempts must be a positive integer.
Browser warning must precede timeout, and browser values must fit the JavaScript
timer range. Invalid generation configuration fails safely with 503 before any
provider call or state mutation.

OpenAI and NVIDIA are explicitly constructed with `max_retries=0`. Bedrock uses
`Config(retries={"mode": "standard", "total_max_attempts": 1})`, overriding AWS
profile/environment retry counts. One application attempt therefore performs at
most one provider SDK request: the default request ceiling is **three total**,
rather than multiplying SDK and application retries. The old NVIDIA
`MAX_RETRIES=5` constant is removed. Existing `provider_configuration` events
show effective timeout/retry settings.

Eligible failures include connection/timeouts, HTTP 408/429/5xx, and Bedrock
throttling, model-not-ready, model-timeout, and temporary service errors.
Authentication, permission, missing credentials/model/configuration, unsupported
parameters (400/422), and unknown exceptions fail immediately. A general
`ValueError` is no longer treated as an invalid generated question. JSON/schema
failures and difficulty mismatches remain eligible within the same attempt limit.

Default retry sleeps are 0.25-0.5 seconds and 0.5-1 second; the configurable ceiling
never exceeds `AI_RETRY_BACKOFF_MAX_SECONDS`. A valid OpenAI-compatible
`Retry-After` delay is honored if it fits the backoff cap and remaining budget.
If it requests more than the cap, the request returns 503 instead of sleeping too
long or retrying earlier than requested. No sleep occurs after the final attempt.
503 retains `{"detail":"AI provider unavailable"}`; invalid-question exhaustion
retains 502 with `{"detail":"AI could not generate a valid question"}`. A budget
check immediately before an attempt returns 503. Existing endpoint response bodies
and reward rules are preserved.

New correlated diagnostic events include `generation_policy`,
`generation_retry_scheduled` (failed attempt and delay),
`generation_retries_exhausted`, `generation_budget_exhausted`,
`generation_retry_suppressed`, and `generation_configuration_failed`.
`generation_attempt_completed` includes a safe `retryable` flag for provider
failures. Headers, prompt/question content, secrets, and exception text remain
excluded.

**Expected waiting time:** normal NVIDIA requests should still finish in a few
seconds. Assuming each provider attempt's total duration follows its read-timeout
allocation, repeated slow failures use about 30 seconds including retry sleeps:
12 + up to 0.5 + 12 + up to 1 + the remaining approximately 4.5 seconds. This is
**not a strict server wall-clock deadline or guaranteed worst case**. SDK timeouts
limit individual socket operations/inactivity, rather than the entire generation.
DNS, connection/write/read phases, streamed response activity, client setup/AWS
credential discovery, and scheduling can extend a call. An already running call is
not interrupted at the budget boundary. Session-lock waiting is outside the
budget and can add time. No abandoned background threads implement a fake timeout.
The browser stops its own request after 45 seconds by default; timer scheduling
in a suspended/throttled browser can also defer UI updates.

If an active call succeeds after the budget, its validated question is still cached.
A failed attempt does not consume its selected category, change difficulty, or
award rewards. The existing session lock covers the complete retry loop. Concurrent
requests for the same unanswered question wait and reuse the cached result; if the
first request fails without a question, a later request can try the same category.

### Browser timeout and Retry recovery

The browser fetches `GET /api/client-config` at startup. This new endpoint returns
only `long_wait_warning_ms` and `request_timeout_ms`; it exposes no provider or
secret configuration. Bootstrap configuration has a five-second timeout and falls
back to the finite 8/45-second defaults when unavailable or invalid.

After the warning threshold, the UI says the question is taking longer than usual.
After an error/timeout it clears its loading state and offers **Retry question**.
Retry calls the same game's question URL; it does not create a new game or
resubmit the previous answer. Rapid clicks while loading are ignored. Warning
timers and HTTP subscriptions are cleaned up on completion, failure, and component
destruction.

A browser timeout unsubscribes from HTTP but **does not guarantee cancellation of
the server's synchronous provider call**. Retry may initially wait behind that
call's session lock. If a valid unanswered question was generated, Retry retrieves
that same question ID without another AI call. This also covers late responses
that the browser did not receive. Generation retries cannot allocate stars;
answers remain single-use and concurrent duplicate submissions still return 409.
POST requests are never retried automatically. If an answer's response is lost,
the server may already have recorded it; replay protection prevents double rewards,
but recovering that lost feedback is outside this question-generation change.
The confirmed stuck-player scenario and a proposed read-only answer receipt API
are documented in [the CR-2 recovery follow-up](ANSWER_RESPONSE_RECOVERY.md).

For SDK semantics, see [official OpenAI error guidance](https://developers.openai.com/api/docs/guides/error-codes),
[Botocore configuration](https://docs.aws.amazon.com/botocore/latest/reference/config.html),
and [Angular HTTP request handling](https://angular.dev/guide/http/making-requests).

### Offline checks

From `backend`, with the configured interpreter:

~~~powershell
python -m unittest discover -s tests -v
python -m py_compile api.py question_generator.py define_ai_provider.py generation_policy.py
~~~

If the repository-root environment is selected rather than `backend/.venv`, use
`..\.venv\Scripts\python.exe` in place of `python`. SDK constructors and provider
calls, clocks, jitter, and backoff sleeps are mocked in request-flow tests.
Constructor regressions additionally instantiate actual OpenAI/NVIDIA SDK clients
with dummy keys and block SDK requests/socket connections. They cover numeric and
structured timeout diagnostics, including remaining-budget limits. The timeout and
retry policy is unchanged by the CR-1 fix. Tests cover transient/permanent
failures, mixed retries/exhaustion, unchanged failure state, lock serialization,
late/browser-abandoned request recovery, and duplicate reward prevention. No AI
keys, paid calls, or remote AI requests are used.

From `frontend`:

~~~powershell
npm.cmd test
npm.cmd run build
~~~

The frontend tests use Node's built-in runner, the installed TypeScript compiler,
and RxJS virtual time with mocked HTTP. They require no running server, browser,
new testing dependency, or real delays. They exercise component behavior and Retry
template wiring; the production Angular build checks template types. A real
DevTools/browser session remains useful to confirm rendering and proxy behavior.
Use `npm.cmd` on Windows when PowerShell's execution policy blocks `npm.ps1`.


## 6. AI Engine v2: completion metadata (Step 1)

Provider text now passes a typed completion-status gate before JSON parsing and
schema validation. See [the result contract and provider mapping](PROVIDER_COMPLETION.md)
for retry behavior, the `provider_completion` diagnostic event, offline coverage,
and provider-specific limitations. Existing timeout/retry defaults, endpoint
response formats, game state rules, and correct-answer handling are preserved.


## 7. AI Engine v2: opt-in Structured Outputs (Step 2)

`AI_STRUCTURED_OUTPUT_MODE=off` preserves the existing provider requests by default.
After verifying the configured model's support, set `AI_STRUCTURED_OUTPUT_MODE=strict`
and, for NVIDIA, explicitly select `NVIDIA_STRUCTURED_OUTPUT_FORMAT=guided_json` or
`json_schema`. There is no automatic fallback on unsupported models or parameters.
See [Structured Outputs configuration and verification](STRUCTURED_OUTPUTS.md)
for provider request shapes, safe diagnostics, rollback to off mode, and SDK/model
limits. Bedrock's first schema compilation can exceed the unchanged interactive
timeout/budget; this change does not extend it. Pydantic and difficulty checks
remain mandatory after the existing completion gate.
