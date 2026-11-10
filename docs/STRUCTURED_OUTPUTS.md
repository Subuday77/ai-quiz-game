# AI Engine v2, Step 2: opt-in native Structured Outputs

## Configuration

Native structured generation is **off by default**. No secret `.env` file is
modified by this change. Add settings to the backend environment, restart FastAPI,
and keep the existing provider, model, and credentials configured locally:

~~~dotenv
AI_STRUCTURED_OUTPUT_MODE=off
NVIDIA_STRUCTURED_OUTPUT_FORMAT=guided_json
~~~

`AI_STRUCTURED_OUTPUT_MODE` accepts only `off` or `strict`. In `off`, SDK request
parameters retain the prior behavior for all three providers, including NVIDIA's
`chat_template_kwargs={"enable_thinking": false}`. The NVIDIA format setting is
ignored in off mode.

To opt in after checking the model's documentation:

~~~dotenv
AI_STRUCTURED_OUTPUT_MODE=strict
~~~

For NVIDIA, explicitly select one documented mechanism for the exact deployment:

~~~dotenv
NVIDIA_STRUCTURED_OUTPUT_FORMAT=guided_json
# Alternative for a model/backend that documents this interface:
# NVIDIA_STRUCTURED_OUTPUT_FORMAT=json_schema
~~~

There is no automatic format switch, plain-text downgrade, model substitution, or
prompt-only substitute. Invalid mode/format fails locally. Provider 400/422 schema
or unsupported-parameter errors fail once through the existing generic 503 API
contract. Check the safe diagnostics for the reason. To return to the previous
behavior, explicitly set `AI_STRUCTURED_OUTPUT_MODE=off` and restart the backend.

## Shared transport schema and application validation

`backend/question_transport_schema.py` defines `quiz_question_v1`. It uses only
objects, integer/string types, required properties, a choice enum, and
`additionalProperties: false` on both the root and answers. The root has exactly
`difficulty`, `question`, `answers`, and `correct_answer`; answers has exactly
required string keys A/B/C/D. All root properties are required.

The transport schema deliberately omits numeric/string/property-count constraints
and does not send `QuizQuestion.model_json_schema()` to providers. It has no
per-question values, difficulty bounds, topic, or language substitutions. Each
request receives a fresh copy of the same schema, preventing mutation from
changing later requests. Bedrock receives a deterministic JSON serialization.
The stable name and schema enable provider caching without generating a different
grammar for every level. Change the schema/name deliberately if the contract evolves.

The processing order remains:

1. Request text, optionally constrained by the native transport schema.
2. Normalize the provider's completion metadata into the existing `ProviderResult`.
3. Gate parsing on explicit completion. Truncation never reaches parsing; it can
   use the existing bounded retry loop. Refusals, safety blocks, tool results,
   and unknown completion states fail without automatic retries.
4. Parse completed JSON, retaining existing Markdown-fence handling.
5. Validate with `QuizQuestion`, including range 0-15, question length, required
   fields, nonempty/distinct answer choices, and extra-property rejection.
6. Check generated difficulty against the requested difficulty.
7. Cache and return the public question, with `correct_answer` kept server-side.

Provider acceptance of a schema cannot bypass these checks. Completed invalid
JSON or invalid questions still use the existing invalid-question retries/502
contract. No generation failure consumes the selected category, advances the game,
or allocates a reward. Session locks and cached unanswered questions are unchanged.

## Provider mechanisms

### OpenAI Responses

The request adds `text.format` with `type="json_schema"`,
`name="quiz_question_v1"`, `strict=true`, and the shared schema object. Existing
model, reasoning, temperature, token limits, timeouts and `max_retries=0` remain
unchanged. A configured model must support this native interface and the existing
sampling parameters. Older SDKs that reject `text` fail without fallback.

See [official OpenAI Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

### NVIDIA Chat Completions

The two formats are exclusive:

- `guided_json` (the strict-mode default): add the schema under
  `extra_body.guided_json`, alongside the existing `chat_template_kwargs`.
  This matches the [NVIDIA NIM 1.15 structured-generation example](https://docs.nvidia.com/nim/large-language-models/1.15.0/structured-generation.html).
- `json_schema`: add `response_format={"type":"json_schema","json_schema":
  {"name":"quiz_question_v1","schema":...}}`. The chat-template extension stays
  in `extra_body`, and no `guided_json` is sent. This shape is shown in
  [NVIDIA's Cosmos NIM structured-output guidance](https://github.com/NVIDIA/cosmos/blob/main/cookbooks/cosmos3/nim/reasoning.md).
  No additional OpenAI `strict` flag is assumed for NVIDIA.

These references establish request shapes, not support for every model hosted at
`integrate.api.nvidia.com`. NIM versions/inference backends differ; older examples
may use `nvext.guided_json`, which this implementation does not send. If your
model requires that wrapper or another mechanism, leave strict mode off until it
is implemented and reviewed. Do not assume `json_object` enforces this schema.

Before enabling, check the configured NVIDIA model's API/model card and deployment
version for schema-constrained generation and the selected parameter location.
The repository does not infer capabilities from model names or guess formats by
sending probe requests. No configured NVIDIA model was called during this change.

### AWS Bedrock Converse

The request adds:

~~~json
{
  "outputConfig": {
    "textFormat": {
      "type": "json_schema",
      "structure": {
        "jsonSchema": {
          "name": "quiz_question_v1",
          "schema": "<JSON-serialized shared schema>"
        }
      }
    }
  }
}
~~~

`schema` is a JSON string, not an object. No extra `strict` field, strict tool
configuration, or model-specific options are added. The installed SDK's Converse
input shape is checked locally before a strict request; missing support emits
`bedrock_sdk_unsupported` and fails without a provider call. Off mode skips that
check. Update Boto3/Botocore using your dependency-management process if necessary;
this patch does not install packages or require a version upgrade.

A model and its region/inference profile must support native structured output;
check the [Bedrock model cards](https://docs.aws.amazon.com/bedrock/latest/userguide/models-at-a-glance.html)
and [Bedrock Structured Outputs guide](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html).
The service remains authoritative for capability checks; an SDK supporting the
field does not prove that an arbitrary model supports it. Unsupported model/schema
validation failures remain non-retryable and do not trigger a downgrade.

Bedrock documents that first-time schema compilation can take **a few minutes**.
That can exceed the existing 12-second provider socket timeout and 30-second soft
generation budget. Compiled grammar caching helps later identical requests, but
this patch does not warm schemas, extend timeouts, or secretly wait longer.
A compatible cold model can therefore still time out. Keep off mode if that
latency does not fit your current interactive budget, or review a separate explicit
policy change. The browser's 45-second timeout and existing recovery limitations
also remain unchanged.

## Diagnostics and real-model verification

Events retain the diagnostic request ID and application attempt number:

| Event | Meaning |
| --- | --- |
| `structured_output_request` | Native request is about to start; records provider, `native_requested`, and selected `output_format`. Off mode reports false/text. |
| `structured_output_result` | Provider returned a normalized completion; classification is `complete`, `truncated`, `refused`, `blocked`, `tool_call`, or `unknown`. |
| `structured_output_failed` | Safe classification: `configuration_error`, `provider_request_rejected`, `sdk_request_rejected`, `transient_failure`, or `permanent_failure`. |

Configuration failures include allowlisted reason codes: `invalid_mode`,
`invalid_nvidia_format`, `unsupported_provider`, or `bedrock_sdk_unsupported`.
No invalid environment values, schema, model IDs, question/answer text, credentials,
headers or raw exception messages are logged. SDK failures record exception type;
OpenAI-compatible HTTP rejections also record status. A provider-rejection event
means the request was rejected; it does not guess which field caused rejection.
Consult model documentation rather than enabling HTTP debug/body logging.

To verify a configured model manually (outside automated tests):

1. Confirm the exact model/deployment documents native schema generation and the
   selected request shape. Keep credentials local.
2. Enable strict mode and select the NVIDIA format when applicable; restart FastAPI.
3. Request a question and follow its `X-Diagnostic-Request-ID`. Confirm
   `native_requested=true`, the intended provider/format, then a `complete` result
   and existing JSON-parse/Pydantic validation success events.
4. Check several difficulties: the schema remains identical while application
   validation checks the requested level. A schema/model 400/422 should show one
   failed attempt, no downgrade, and no game-state mutation.
5. If unsupported, return to off mode explicitly. If the native parameter is
   silently ignored by a deployment, logs cannot establish constrained-decoding
   enforcement. Request acceptance, valid JSON, and compliant samples alone are
   not proof; confirm support through the model/deployment documentation or its
   authorized server-side telemetry. Do not claim all NVIDIA models are verified.

## Limits and offline checks

JSON Schema constrains shape, not factual correctness, ambiguity, answer accuracy,
or semantic difficulty. Pydantic adds application constraints but is not a factual
quality checker. Issue #3/#4 behavior, frontend, game rules, databases, tool loops,
and the Step 1 completion contract are outside this step.

Defaults remain three shared attempts, SDK retries disabled, a 12-second provider
socket limit, the 30-second soft generation budget, and the current bounded
backoff/jitter. HTTP 429/5xx/connection timeouts remain eligible where previously
supported. No separate schema retry loop or altered request deadline is added.

From `backend`, using the configured interpreter:

~~~powershell
python -m unittest discover -s tests -v
python -m compileall -q .
~~~

The local environment is `..\.venv\Scripts\python.exe` (OpenAI 3.16.2,
Boto3/Botocore 1.43.98). Tests mock provider clients, use local SDK metadata to
validate the Bedrock payload, and never call paid/remote AI services. They cover
the exact portable schema and Pydantic drift checks, unchanged off payloads, all
native formats, unsupported settings/SDK/models, no downgrade, safety/completion
gating, validation, retries, state preservation and diagnostic redaction.
Real-model compatibility and cold-schema latency still require manual verification.


The optional [quality checker](QUALITY_CHECKER.md) reuses these mechanisms with a
separate internal `quality_review` contract and stable `quality_review_v1` schema.
Generator requests retain `quiz_question_v1`. Native schema selection is explicit
per request; off-mode generator payloads remain unchanged. Verify model support
for each role's schema before combining strict output with enforced review.


Reviewer configuration is independent: `AI_QUALITY_STRUCTURED_OUTPUT_MODE=off|strict`
overrides `AI_STRUCTURED_OUTPUT_MODE` for the reviewer only; when absent it inherits
the generator mode. Generator requests are unchanged. Provider/model overrides must
be paired as `AI_QUALITY_PROVIDER` and `AI_QUALITY_MODEL_ID`. Strict review uses
`quality_review_v1` on that configured model; off review still passes completion,
JSON and Pydantic checks. Invalid reviewer settings follow audit/enforce failure
policy and never trigger silent downgrade. NVIDIA roles share the configured
`NVIDIA_STRUCTURED_OUTPUT_FORMAT` and retain existing chat-template options.

The example uses strict Super generation with Nano review in off mode; this avoids
assuming equal native schema support but does not guarantee Nano accepts the other
parameters or produces valid JSON. Verify actual deployment support separately.
Diagnostic events identify role, safe model identity, mode, format and contract.
