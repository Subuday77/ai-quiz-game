# AI Engine v2, Step 1: provider completion status

The internal `ProviderResult` in `backend/provider_result.py` is a frozen dataclass
with exactly three fields: `output_text: str | None`, `completion: Completion`, and
`provider_reason: str | None`. `Completion` is a typed enum with `complete`,
`truncated`, `refused`, `blocked`, `tool_call`, and `unknown`. The contract and its
typed completion exceptions use only Python's standard library, without SDK types.

The provider adapters return this contract rather than SDK responses. They retain
text exactly as returned, joining text blocks in order for OpenAI and Bedrock.
They do not return reasoning text, tool arguments, refusal explanations, or raw
provider envelopes. Optional Markdown fences are removed by the question generator
only after it checks completion, before JSON parsing. Pydantic still validates the
parsed question in the existing API flow; completion alone does not prove valid
JSON, schema correctness, factual accuracy, or suitable difficulty.

## Normalization rules

| Provider | Metadata | Normalized completion |
| --- | --- | --- |
| OpenAI Responses | `status=completed`, no incomplete reason, finished messages and supported output types | `complete` |
| OpenAI Responses | `incomplete_details.reason=max_output_tokens` | `truncated` |
| OpenAI Responses | `incomplete_details.reason=content_filter` | `blocked` |
| OpenAI Responses | A message content block with `type=refusal` | `refused`, including when the overall response status is completed |
| OpenAI Responses | Function/custom/built-in tool call or MCP approval-request output | `tool_call`; no tool is executed |
| NVIDIA Chat Completions | First choice `finish_reason=stop` | `complete` |
| NVIDIA Chat Completions | `length` | `truncated` |
| NVIDIA Chat Completions | `content_filter` | `blocked` |
| NVIDIA Chat Completions | Non-null refusal string in the assistant message | `refused` |
| NVIDIA Chat Completions | `tool_calls` / deprecated `function_call`, or actual message tool-call payload | `tool_call` |
| Bedrock Converse | `stopReason=end_turn` / `stop_sequence` | `complete` |
| Bedrock Converse | `max_tokens` / `model_context_window_exceeded` | `truncated` |
| Bedrock Converse | `guardrail_intervened` / `content_filtered` | `blocked` |
| Bedrock Converse | `tool_use` / `malformed_tool_use`, or actual `toolUse` content | `tool_call` |
| All | Missing or unrecognized completion metadata | `unknown`; never assume success from parseable text |

Safety/refusal/tool evidence overrides text that otherwise looks complete. A safety
block takes precedence over tool payloads. A refusal or unexpected tool call is
not reclassified as retryable truncation merely because token-limit metadata is
also present. OpenAI reasoning items may accompany assistant text without making
it a tool call; unfamiliar output types and unfinished messages fail closed.

`provider_reason` preserves NVIDIA's original finish reason and Bedrock's original
stop reason. For OpenAI it preserves `incomplete_details.reason` when present,
otherwise the original top-level `status`. Thus a refused completed OpenAI response
can legitimately have `completion=refused` and `provider_reason=completed`; the
refusal content discriminator is separate evidence. No fabricated provider
`refused` status is introduced. A missing reason stays null and unfamiliar strings
are retained as received.

## Parsing gate and retry behavior

`completed_text` runs before JSON parsing. It raises typed exceptions instead of
letting incomplete text reach the JSON/Pydantic validators:

- `TruncatedGenerationError` and `EmptyGenerationError` are eligible for the existing
  application retry loop. Empty/whitespace/missing text is rejected even when the
  provider explicitly reports completion.
- `RefusedGenerationError`, `BlockedGenerationError`, `UnexpectedToolCallError`, and
  `UnknownCompletionError` are non-retryable. They return the existing generic 503
  response without attempts to circumvent safety or run tools.

Completion failures and existing transient/validation failures share the same
three-attempt default and 30-second soft generation budget. SDK retries remain
disabled and the provider socket timeout remains 12 seconds by default. Limits,
backoff and jitter are unchanged; retries do not increase the token limit.
Completion retry exhaustion returns the existing provider-unavailable 503 contract.
A completed response with malformed JSON or schema still follows the existing
invalid-question retry/502 path. Failed requests do not consume the selected
category, advance difficulty, or award achievements. Session locking and cached
unanswered questions remain unchanged.

## Diagnostics

Each returned provider response produces one `provider_completion` event containing
`provider`, normalized `completion`, and original `provider_reason`, with the same
request ID and attempt number as the provider timing events. No output text,
refusal explanation, tool arguments, prompt, raw response, headers, or credentials
are logged. Provider duration excludes normalization and parsing. Completion
exception messages contain the enum value rather than output or provider reason;
existing API failure logging continues to record only exception class/metadata.

## Provider-specific limitations

- OpenAI's `failed`, `cancelled`, `queued`, `in_progress`, and incomplete responses
  without a recognized reason normalize to `unknown`. Other documented incomplete
  reasons such as `max_messages` or `steered` are conservatively unknown rather
  than guessed to be token truncation. HTTP/SDK exceptions retain their existing
  transient/permanent classification.
- NVIDIA uses the first Chat Completions choice. Hosted models may omit or extend
  completion metadata; unknown reasons fail closed. The OpenAI-compatible refusal
  field is used only when actually supplied, without guessing from free-form text.
- Bedrock does not expose a universal refusal stop reason. Unmarked textual model
  refusals cannot be distinguished from ordinary text at this layer. Do not invent
  `stopReason=refusal`. `malformed_model_output` remains unknown/non-retryable;
  only documented stop reasons are mapped.
- This is a non-streaming result contract. A completed status can still accompany
  empty text, and a stopped model can still produce invalid JSON. No quality
  checker, native Structured Outputs, streaming framework, or tool loop is added.

## Offline verification

Provider fixtures include realistic completion metadata. Normalization tests cover
successes (including SDK response model fixtures), partial text, refusal/filtering,
tools, missing text/choices/metadata, unfamiliar reasons, and safety precedence.
Mocked SDK integration tests verify correlation/redaction, rejection before parsing
and Pydantic, retries, shared exhaustion/budget, and unchanged failure state.
Existing real-client constructor regressions use dummy keys with requests blocked.
No AI request is sent.

From `backend`, using the configured Python environment:

~~~powershell
python -m unittest discover -s tests -v
python -m compileall -q .
~~~

The development interpreter in this workspace is `..\.venv\Scripts\python.exe`.

Sources checked for the mappings: [OpenAI response completion/refusal examples](https://developers.openai.com/api/docs/guides/structured-outputs),
[NVIDIA OpenAI-compatible Chat Completions](https://docs.nvidia.com/nim/large-language-models/latest/get-started/quickstart.html),
and [Bedrock Converse stop reasons](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html).
OpenAI SDK type definitions were also checked locally; mappings should be reviewed
if providers introduce new response variants.
