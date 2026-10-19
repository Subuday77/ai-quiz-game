# Optional AI question quality checker

The checker adds a separate evaluation context after generator completion checks,
JSON parsing, Pydantic validation and requested-difficulty validation. The API holds
the same session lock throughout generation and review. Only an approved candidate
(or an audit-mode candidate) receives an ID, enters the unanswered-question cache
and consumes a category. Rejected questions cannot advance difficulty or allocate
rewards. The question API exposes neither the designated correct answer nor review
details. Answer feedback retains its existing contract.

## Configuration and rollout

```dotenv
AI_QUALITY_MODE=off
```

| Mode | Behavior |
| --- | --- |
| `off` (default) | Existing generation behavior; no reviewer calls or local lexical checks. |
| `audit` | Review and record safe findings; review decisions, failures or exhausted review time do not reject a structurally valid question. |
| `enforce` | Require valid approval; reject unsuitable or uncertain questions and fail closed on reviewer failure. |

Only these exact lowercase values are accepted. Invalid configuration fails before
uncached generation, with the existing generic HTTP 503. Existing cached questions
are returned without regeneration or retroactive review. To assess enforcement,
start a new local game; changing mode does not invalidate an unanswered question.
Restart the backend after editing environment settings. Roll back with
`AI_QUALITY_MODE=off`; no frontend changes are needed.

Start with `audit` and inspect the correlated decision/issue-code diagnostics,
latency, transient failures and escalation rate. In controlled local testing,
manually assess questions you see against trusted references; regular logs cannot
serve as a question/rationale review dataset because those fields are redacted.
Test challenging questions, supported languages and the configured model before
opting into `enforce`. Enabling review makes an additional billable request per
valid candidate, potentially doubles request count, and increases latency. Audit
is observational: unsuitable candidates can still reach players.

## Review contract and decisions

The separate system prompt asks the reviewer to solve independently before comparing
its answer to the supplied key. It checks factual premises/answers, terminology,
unsupported explanations, answer uniqueness, ambiguity/scope, lexical or formatting
leakage, significant difficulty mismatch and language/localization problems. All
candidate fields are explicitly treated as untrusted data, never instructions.

The validated `QualityReview` model forbids extra fields and requires:

- `decision`: `accept`, `regenerate`, or `escalate`.
- `issue_codes`: known enum values `factual_error`, `multiple_correct`, `ambiguous`,
  `answer_leak`, `difficulty_mismatch`, `language_problem`, `needs_verification`, `other`.
- `independent_answer`: `A`, `B`, `C`, `D`, or `unknown`.
- `rationale`: a nonblank internal explanation of at most 600 characters.

Duplicate issue codes are invalid. Approval requires no issues and a known answer.
An otherwise accepting verdict whose independent answer disagrees with the supplied
key becomes an escalation with `needs_verification`; disagreement is not proof of
a factual error. Nonaccepting decisions require issue codes. Rationale and raw
review output are neither returned nor stored in regular diagnostics.

In enforce mode, `accept` permits caching; `regenerate` consumes the current
attempt and regenerates within the existing shared loop; `escalate` immediately
stops with generic HTTP 503, without automatic repeated reviews or acceptance.
Repeated rejection exhausts the existing loop with generic HTTP 502. There is no
human escalation queue in this MVP: escalation means withhold the question. A
manual retry is a new request with its own bounded generation budget, as before.

## Provider integration and validation

`OutputContract` has just two internal values: `question` and `quality_review`.
It selects stable transport schemas named `quiz_question_v1` and
`quality_review_v1`. Question schema and off-mode provider payloads are preserved.
The review transport schema is portable JSON Schema (all fields required, root
`additionalProperties: false`); Pydantic separately enforces semantic consistency
and rationale limits. Native output never bypasses application validation.

The reviewer reuses `LLMClient` and `ProviderResult`. Completion normalization
precedes review JSON parsing: truncated, refused, blocked, tool-call and unknown
results cannot become approval. Empty output, malformed JSON, invalid fields or
contradictory verdicts cannot implicitly accept. There is no tool execution.

`AI_STRUCTURED_OUTPUT_MODE` applies independently of quality mode. With `off`,
review still requests JSON in its separate prompt and validates it. With `strict`,
OpenAI uses `text.format`, NVIDIA uses the configured `guided_json` or `json_schema`
mechanism, and Bedrock uses `outputConfig.textFormat`, selecting the review schema
instead of the question schema. NVIDIA chat-template options remain unchanged.
Unsupported models, SDKs or schema parameters fail without downgrade; 400/422 and
other permanent request errors are not retried. Audit logs such failures and keeps
the question; enforce rejects it. See [provider mechanisms and limitations](STRUCTURED_OUTPUTS.md).
The configured model must support **both** schemas. Bedrock can compile each schema
on first use; this may exceed the unchanged interactive timeout.

## Shared retry and timing limits

Default limits remain three generation attempts and a 30-second soft budget. An
attempt includes one generator call, validation and at most one review call.
Validation failure never calls the reviewer. There is no checker retry loop:
a transient review timeout/connection/429/eligible 5xx failure consumes the attempt,
and the existing bounded backoff/jitter loop may regenerate and review a new
candidate. Truncation and empty completions retain existing transient eligibility.
Malformed review JSON/schema, refusal, blocks, tools, unknown completion and
permanent errors stop enforcement immediately. Retry-After limits are preserved.
SDK retries remain disabled for all providers.

Before creating the reviewer client, the checker checks remaining time and
allocates socket timeout `min(AI_PROVIDER_TIMEOUT_SECONDS, remaining_budget)`.
It checks the budget again after setup. Immediately before the SDK request, after
schema preparation, it recomputes the remaining allocation. OpenAI and NVIDIA use
the SDK resource method's `timeout` override, so the request uses the fresh value
rather than the timeout captured during construction.

Bedrock Converse does not expose an equivalent request-level socket override.
The existing transport's configured connect **and** read limits must already fit
within the fresh allocation. If either exceeds it, no request starts: diagnostics
report `timeout_allocation_unavailable`, audit retains the candidate, and enforce
fails closed with generic HTTP 503 without automatic retry. The client is not
reconstructed, and neither its Config metadata nor private transport internals are
mutated. Even a small setup delay can suppress review when Bedrock was constructed
with a timeout equal to all remaining time. This conservative limitation avoids
claiming an adjustment that the SDK transport did not apply.

If the budget is exhausted during client or schema setup, no provider request
starts; audit retains the candidate and enforce fails closed. These failure paths
restore the caller's timeout context, as do success and provider exceptions.
The current defaults are unchanged: provider sockets 12 seconds, browser HTTP
45 seconds, long-wait warning 8 seconds.

This is **not a strict wall-clock deadline**: socket limits, client setup, multiple
socket phases, parsing and already-running requests can overrun the soft budget.
A successful in-flight review can finish after the budget and be cached. No
abandoned background threads simulate cancellation. A browser timeout need not
cancel the synchronous server request. A retry waits on the same session lock
and reuses a completed unanswered question rather than generating a duplicate.

## Deterministic findings and diagnostics

A separate conservative local check normalizes Unicode (NFKC/casefold), extracts
letter tokens and considers long words/names (10-64 characters) in paired double,
curly or guillemet quotes. It flags a token only when it closely resembles a word
in the designated answer (similarity at least 0.9, length difference at most two)
and has no comparable match in any distractor. Ordinary unquoted shared topic
words and short common words are ignored. No historical names are hardcoded.

Local `answer_leak` findings have `source=local_precheck` and are distinct from
model judgments. Enforce regenerates without spending a reviewer call on a local
rejection. Audit still reviews and includes `local_issue_codes` in diagnostics.
This deliberately low-recall heuristic can miss synonyms, morphological variants,
transliteration, nicknames and less similar names; long quoted terms may still
cause false positives. It is not a general linguistic or factual validator.

Events `quality_review_started`, `quality_review_completed`, `quality_review_failed`,
`quality_question_rejected`, `quality_review_skipped` retain request ID and attempt
correlation. Depending on event, safe fields include mode, provider, duration,
decision, issue codes, source, reason and failure classification. Existing native
output diagnostics also include the selected output contract.
`provider_timeout_allocated` distinguishes an OpenAI/NVIDIA `request_override` from
Bedrock `existing_socket_limits` and records the applied limits safely;
`provider_timeout_unavailable` records a suppressed Bedrock request. These events
retain request/attempt correlation. Questions, choices,
keys, independent answers, rationale, prompts, credentials, full schemas, raw
provider responses and exception messages are excluded.

## Limits and offline regression coverage

The reviewer initially uses the same provider/model as the generator. A separate
prompt gives independent evaluation context, **not independent factual authority**.
Both calls can share biases and knowledge gaps. Although instructed to solve first,
the model sees the supplied answer key in the same request and can be biased by it.
Prompt instructions mitigate but cannot guarantee resistance to malicious candidate
text. No external research or reference verification occurs. A reviewer can
hallucinate, reject valid questions or approve invalid ones. JSON Schema assures
structure, not truth, fairness, difficulty calibration or semantic quality.

`backend/tests/fixtures/historical_nickname.json` preserves the Russian prince
question with the incorrect nickname. A mocked verdict identifies the wrong
nickname, potentially unsupported explanation and lexical giveaway, and tests
that enforcement rejects it without consuming a category, advancing difficulty
or allocating a reward. The conservative local heuristic does not claim to detect
that particular nickname pair. A mocked reviewer proves pipeline behavior, **not**
that a live model will catch every historical mistake.

Offline tests mock all generation/review requests and cover modes, configuration,
completion gates, malformed verdicts, shared retry/backoff limits, budget allocation,
state preservation, cached/concurrent recovery, single-use rewards, diagnostics
redaction and native schema selection for both contracts. No paid or remote AI
requests are made. Run from `backend`:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```
