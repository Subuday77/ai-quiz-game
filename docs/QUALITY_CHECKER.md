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

Only these exact lowercase quality modes are accepted. An invalid `AI_QUALITY_MODE`
fails before uncached generation, with the existing generic HTTP 503. Invalid
reviewer-only provider/model/schema settings are review failures: audit records
them and retains a structurally valid candidate; enforce fails closed without retry. Existing cached questions
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

## Independent reviewer configuration

The generator still uses `LLM_PROVIDER`, the corresponding `*_MODEL_ID` and
`AI_STRUCTURED_OUTPUT_MODE`. Reviewer-only settings are resolved explicitly;
there are no temporary environment changes or generator/global model mutations.

```dotenv
LLM_PROVIDER=nvidia
NVIDIA_MODEL_ID=nvidia/nemotron-3-super-120b-a12b
AI_STRUCTURED_OUTPUT_MODE=strict
AI_QUALITY_MODE=audit
AI_QUALITY_PROVIDER=nvidia
AI_QUALITY_MODEL_ID=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
AI_QUALITY_STRUCTURED_OUTPUT_MODE=off
NVIDIA_STRUCTURED_OUTPUT_FORMAT=guided_json
```

`AI_QUALITY_PROVIDER` accepts `nvidia`, `openai` or `bedrock`; it must be paired
with a nonblank `AI_QUALITY_MODEL_ID`. Remove both to inherit the generator provider
and its provider-specific model. A missing partner, empty setting, unknown provider
or blank model is a safe configuration failure, not a guessed value. Changing
provider uses its existing credentials and region settings; the generator model
variable for that provider is not required when an explicit reviewer model is set.
`NVIDIA_FALLBACK_MODEL_ID` remains informational and is never selected automatically.

`AI_QUALITY_STRUCTURED_OUTPUT_MODE=off|strict` independently selects reviewer native
output. If absent, it inherits `AI_STRUCTURED_OUTPUT_MODE`; empty or invalid values
are errors. `off` still requires reviewer JSON and Pydantic validation. The example
uses strict Super generation and prompt-guided Nano review, **not a claim that Nano
supports the same schema or reasoning features**. NVIDIA's existing
`chat_template_kwargs.enable_thinking=false` is preserved. Both NVIDIA roles share
`NVIDIA_STRUCTURED_OUTPUT_FORMAT` if strict; unsupported combinations fail without
fallback. Verify deployed model support manually. No live accuracy or Super/Nano
benchmark is included here.

With quality mode off, reviewer configuration is not resolved and no reviewer
client is constructed, even if reviewer overrides are invalid. Other modes retain
the same three-attempt loop, 30-second soft budget and provider-specific dispatch
timeout handling described below. A different model can reduce correlated errors,
but does not provide independent factual authority or verified references.

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

The reviewer uses `AI_QUALITY_STRUCTURED_OUTPUT_MODE`, inheriting
`AI_STRUCTURED_OUTPUT_MODE` only when absent. With `off`,
review still requests JSON in its separate prompt and validates it. With `strict`,
OpenAI uses `text.format`, NVIDIA uses the configured `guided_json` or `json_schema`
mechanism, and Bedrock uses `outputConfig.textFormat`, selecting the review schema
instead of the question schema. NVIDIA chat-template options remain unchanged.
Unsupported models, SDKs or schema parameters fail without downgrade; 400/422 and
other permanent request errors are not retried. Audit logs such failures and keeps
the question; enforce rejects it. See [provider mechanisms and limitations](STRUCTURED_OUTPUTS.md).
Each role's configured model must support its own schema when strict. Bedrock can compile each schema
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

A separate conservative local check first matches the **complete designated answer**
as a contiguous Unicode token sequence in the question. NFKD normalization,
casefolding and removal of combining/format marks tolerate accents, Hebrew vowel
marks, case and spacing differences; punctuation supplies token boundaries. It
does not match inside longer words. Multiword phrases need at least five token
characters; ordinary single words need at least four letters. Exact quoted answers
can be as short as two characters. Matches are withheld if another full option
appears in the question or contains the same answer phrase. Sharing only a topic
word from a longer answer is not enough.

The previous approximate check remains deliberately narrow: long words/names
(10-64 characters) in paired double, curly or guillemet quotes, similarity at least
0.9, length difference at most two, and no comparable distractor match. No
historical names or per-question facts are hardcoded.

Local `answer_leak` findings have `source=local_precheck`, distinct from model
judgments. Enforce regenerates without a reviewer request for deterministic
rejection; audit still reviews and includes `local_issue_codes`. Literal repetition
of `Первый канал` is covered. Necessary topic references, tiny words, negated clues,
multiple mentioned options and linguistic context can still create missed cases
or false positives; this is not a full language parser or a guarantee of quality.

Both prompts now ask whether a player can identify the correct option from wording
alone, without knowing the fact. They explicitly address literal/quoted answers,
obvious morphological derivatives (`мантия` / `мантийные`), missing context with
multiple defensible answers, and subjective cultural assumptions. They apply to
English, Russian, Hebrew, German, French, Spanish and Italian. Morphology,
translation, synonyms and semantic associations are left to the AI review, rather
than a broad seven-language string heuristic. Conventional unique cultural or
religious answers should be accepted when properly scoped; the prompt does not
instruct blanket rejection of such topics.

Events `quality_review_started`, `quality_review_completed`, `quality_review_failed`,
`quality_question_rejected`, `quality_review_skipped` retain request ID and attempt
correlation. Depending on event, safe fields include mode, provider, duration,
decision, issue codes, source, reason and failure classification. Existing native
output diagnostics also include the selected output contract.
`provider_timeout_allocated` distinguishes an OpenAI/NVIDIA `request_override` from
Bedrock `existing_socket_limits` and records the applied limits safely;
`provider_timeout_unavailable` records a suppressed Bedrock request. These events
retain request/attempt correlation. Provider configuration/request/completion and
native-output diagnostics also identify `role=generator|reviewer`, provider, a
safe model identity, and (for native-output events) structured-output mode/format.
The two public NVIDIA IDs in the example are allowlisted for verbatim `model_id`
logging, making Super generation and Nano review distinguishable. Other model IDs,
including custom names/ARNs, are represented by a stable 12-hex SHA-256
`model_fingerprint` with `model_id=null`; no arbitrary model configuration is echoed. Questions, choices,
keys, independent answers, rationale, prompts, credentials, full schemas, raw
provider responses and exception messages are excluded.

## Limits and offline regression coverage

Without overrides the reviewer uses the same provider/model as the generator;
with a paired override it uses the chosen independent provider/model. A separate
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

`quality_examples.json` adds positive/negative literal leakage cases in all seven
languages, the mantle derivative, ambiguous Bolivian capitals, the subjective
borscht drink, closest-planet trivia and scoped cultural/religious questions.
Deterministic tests assert lexical behavior only; mocked AI verdicts assert pipeline
acceptance/rejection and state preservation, not live model judgment accuracy.

Offline tests mock all generation/review requests and cover modes, configuration,
completion gates, malformed verdicts, shared retry/backoff limits, budget allocation,
state preservation, cached/concurrent recovery, single-use rewards, diagnostics
redaction and native schema selection for both contracts. No paid or remote AI
requests are made. Run from `backend`:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```
