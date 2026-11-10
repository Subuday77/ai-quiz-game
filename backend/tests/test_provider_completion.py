"""Offline provider-envelope normalization and completion-gate integration tests."""

import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion
from openai.types.responses import Response

from api import app, sessions
from define_ai_provider import (LLMClient, normalize_bedrock_response,
                                normalize_nvidia_response, normalize_openai_response)
from generation_policy import is_transient
from provider_result import (BlockedGenerationError, Completion, EmptyGenerationError,
                             ProviderResult, RefusedGenerationError, TruncatedGenerationError,
                             UnexpectedToolCallError, UnknownCompletionError, completed_text)
from test_api import QUESTION


TEXT = json.dumps(QUESTION)
SENSITIVE = "do-not-log-prompt-refusal-tool-arguments-or-secret"


# Tool-related ResponseOutputItem discriminators reviewed in OpenAI SDK 3.16.2.
OPENAI_TOOL_OUTPUT_TYPES = (
    "function_call", "custom_tool_call", "computer_call", "web_search_call",
    "file_search_call", "code_interpreter_call", "image_generation_call",
    "local_shell_call", "shell_call", "apply_patch_call", "mcp_call",
    "tool_search_call", "mcp_approval_request", "function_call_output",
    "custom_tool_call_output", "computer_call_output", "tool_search_output",
    "local_shell_call_output", "shell_call_output", "apply_patch_call_output",
    "mcp_list_tools", "mcp_approval_response", "program", "program_output",
    "additional_tools",
)


def openai_response(status="completed", reason=None, text=TEXT, *, refusal=None, extra=()):
    content = [] if text is None else [NS(type="output_text", text=text)]
    if refusal is not None:
        content.append(NS(type="refusal", refusal=refusal))
    return NS(status=status, incomplete_details=None if reason is None else NS(reason=reason),
              output=[NS(type="message", status="completed", content=content), *extra])


def nvidia_response(reason="stop", text=TEXT, *, refusal=None, tools=None):
    return NS(choices=[NS(index=0, finish_reason=reason, message=NS(
        role="assistant", content=text, refusal=refusal, tool_calls=tools, function_call=None))])


def bedrock_response(reason="end_turn", text=TEXT, *, extra=()):
    content = [] if text is None else [{"text": text}]
    return {"stopReason": reason, "output": {"message": {
        "role": "assistant", "content": [*content, *extra]}}}


NORMALIZERS = {"openai": normalize_openai_response, "nvidia": normalize_nvidia_response,
               "bedrock": normalize_bedrock_response}
RESPONSES = {"openai": openai_response, "nvidia": nvidia_response, "bedrock": bedrock_response}


class NormalizationTests(unittest.TestCase):
    def test_openai_success_with_real_sdk_response_model(self):
        response = Response.model_construct(status="completed", incomplete_details=None, output=[
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": TEXT[:20], "annotations": []},
                {"type": "output_text", "text": TEXT[20:], "annotations": []}]}])
        self.assertEqual(normalize_openai_response(response),
                         ProviderResult(TEXT, Completion.COMPLETE, "completed"))

    def test_nvidia_success_with_real_chat_completion_model(self):
        response = ChatCompletion.model_validate({
            "id": "offline", "created": 0, "model": "offline", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": TEXT, "refusal": None}}]})
        self.assertEqual(normalize_nvidia_response(response),
                         ProviderResult(TEXT, Completion.COMPLETE, "stop"))

    def test_bedrock_success_joins_text_blocks_and_supports_stop_sequence(self):
        for reason in ("end_turn", "stop_sequence"):
            with self.subTest(reason=reason):
                response = bedrock_response(reason, TEXT[:20], extra=[
                    {"reasoningContent": {"reasoningText": {"text": SENSITIVE}}},
                    {"text": TEXT[20:]}])
                self.assertEqual(normalize_bedrock_response(response),
                                 ProviderResult(TEXT, Completion.COMPLETE, reason))

    def test_truncation_metadata_is_preserved_even_with_valid_json(self):
        cases = [("openai", openai_response("incomplete", "max_output_tokens"), "max_output_tokens"),
                 ("nvidia", nvidia_response("length"), "length"),
                 ("bedrock", bedrock_response("max_tokens"), "max_tokens"),
                 ("bedrock", bedrock_response("model_context_window_exceeded"),
                  "model_context_window_exceeded")]
        for provider, raw, reason in cases:
            with self.subTest(provider=provider, reason=reason):
                result = NORMALIZERS[provider](raw)
                self.assertEqual(result, ProviderResult(TEXT, Completion.TRUNCATED, reason))
                with self.assertRaises(TruncatedGenerationError) as error:
                    completed_text(result)
                self.assertTrue(is_transient(error.exception))

    def test_refusal_content_overrides_completed_status(self):
        cases = [(normalize_openai_response, openai_response(refusal=SENSITIVE), "completed"),
                 (normalize_nvidia_response, nvidia_response(refusal=SENSITIVE), "stop"),
                 (normalize_nvidia_response, nvidia_response(refusal=""), "stop")]
        for normalize, raw, reason in cases:
            with self.subTest(provider=normalize.__name__):
                result = normalize(raw)
                self.assertEqual(result.completion, Completion.REFUSED)
                self.assertEqual(result.provider_reason, reason)
                with self.assertRaises(RefusedGenerationError) as error:
                    completed_text(result)
                self.assertFalse(is_transient(error.exception))
                self.assertNotIn(SENSITIVE, str(error.exception))
                self.assertNotIn(TEXT, str(error.exception))

    def test_content_filtering_and_guardrails_are_never_retried(self):
        cases = [("openai", openai_response("incomplete", "content_filter"), "content_filter"),
                 ("nvidia", nvidia_response("content_filter"), "content_filter"),
                 ("bedrock", bedrock_response("content_filtered"), "content_filtered"),
                 ("bedrock", bedrock_response("guardrail_intervened"), "guardrail_intervened")]
        for provider, raw, reason in cases:
            with self.subTest(provider=provider, reason=reason):
                result = NORMALIZERS[provider](raw)
                self.assertEqual(result.completion, Completion.BLOCKED)
                self.assertEqual(result.provider_reason, reason)
                with self.assertRaises(BlockedGenerationError) as error:
                    completed_text(result)
                self.assertFalse(is_transient(error.exception))

    def test_openai_tools_are_rejected_even_with_completed_text(self):
        for kind in OPENAI_TOOL_OUTPUT_TYPES:
            for text in (None, TEXT):
                with self.subTest(kind=kind, with_text=text is not None):
                    result = normalize_openai_response(openai_response(text=text, extra=[
                        NS(type=kind, arguments=SENSITIVE)]))
                    self.assertEqual(result.completion, Completion.TOOL_CALL)
                    self.assertEqual(result.output_text, text)
                    self.assertEqual(result.provider_reason, "completed")
                    with self.assertRaises(UnexpectedToolCallError) as raised:
                        completed_text(result)
                    self.assertFalse(is_transient(raised.exception))

    def test_openai_tool_items_preserve_refusal_and_filter_precedence(self):
        for kind in OPENAI_TOOL_OUTPUT_TYPES:
            cases = [
                (openai_response(refusal=SENSITIVE, extra=[NS(type=kind)]), Completion.REFUSED),
                (openai_response("incomplete", "content_filter", refusal=SENSITIVE,
                                 extra=[NS(type=kind)]), Completion.BLOCKED),
            ]
            for response, expected in cases:
                with self.subTest(kind=kind, expected=expected):
                    self.assertEqual(normalize_openai_response(response).completion, expected)

    def test_nvidia_tool_finish_reasons_and_payloads(self):
        for reason, tools in [("tool_calls", None), ("function_call", None),
                              ("stop", [NS(type="function", function=NS(arguments=SENSITIVE))])]:
            with self.subTest(reason=reason):
                result = normalize_nvidia_response(nvidia_response(reason, tools=tools))
                self.assertEqual(result.completion, Completion.TOOL_CALL)
                self.assertEqual(result.provider_reason, reason)

    def test_bedrock_tools_and_malformed_tool_use(self):
        for reason, extra in [("tool_use", []), ("malformed_tool_use", []),
                              ("end_turn", [{"toolUse": {"input": SENSITIVE}}])]:
            with self.subTest(reason=reason):
                result = normalize_bedrock_response(bedrock_response(reason, extra=extra))
                self.assertEqual(result.completion, Completion.TOOL_CALL)
                self.assertEqual(result.provider_reason, reason)

    def test_missing_unknown_and_nonterminal_metadata_stays_unknown(self):
        cases = [("openai", openai_response(status=status), status)
                 for status in (None, "future_status", "incomplete", "failed", "cancelled", "queued", "in_progress")]
        cases += [("openai", openai_response("incomplete", reason), reason)
                  for reason in ("future_reason", "max_messages", "steered")]
        cases += [(provider, RESPONSES[provider](reason), reason)
                  for provider in ("nvidia", "bedrock") for reason in (None, "future_reason")]
        cases += [("bedrock", bedrock_response("malformed_model_output"), "malformed_model_output")]
        for provider, raw, reason in cases:
            with self.subTest(provider=provider, reason=reason):
                result = NORMALIZERS[provider](raw)
                self.assertEqual(result.completion, Completion.UNKNOWN)
                self.assertEqual(result.provider_reason, reason)
                with self.assertRaises(UnknownCompletionError) as error:
                    completed_text(result)
                self.assertFalse(is_transient(error.exception))

    def test_missing_response_fields_do_not_raise_index_or_attribute_errors(self):
        cases = [(normalize_openai_response, NS()), (normalize_nvidia_response, NS()),
                 (normalize_nvidia_response, NS(choices=[])),
                 (normalize_nvidia_response, NS(choices=[NS()])),
                 (normalize_bedrock_response, {}), (normalize_bedrock_response, {"output": None})]
        for normalize, raw in cases:
            with self.subTest(normalizer=normalize.__name__, raw=raw):
                self.assertEqual(normalize(raw), ProviderResult(None, Completion.UNKNOWN, None))

    def test_completed_but_empty_or_missing_text_has_typed_retryable_error(self):
        for provider in RESPONSES:
            for text in (None, "", "   "):
                with self.subTest(provider=provider, text=text):
                    result = NORMALIZERS[provider](RESPONSES[provider](text=text))
                    self.assertEqual(result.completion, Completion.COMPLETE)
                    with self.assertRaises(EmptyGenerationError) as error:
                        completed_text(result)
                    self.assertTrue(is_transient(error.exception))

    def test_unknown_openai_output_or_unfinished_message_is_not_accepted(self):
        for kind in ("future_output_type", "future_tool_call", "future_tool_output", "compaction"):
            with self.subTest(kind=kind):
                response = openai_response(extra=[NS(type=kind)])
                self.assertEqual(normalize_openai_response(response).completion, Completion.UNKNOWN)
        response = openai_response()
        response.output[0].status = "incomplete"
        self.assertEqual(normalize_openai_response(response).completion, Completion.UNKNOWN)

    def test_safety_and_tools_override_truncation_or_text(self):
        self.assertEqual(normalize_openai_response(openai_response(
            "incomplete", "max_output_tokens", refusal=SENSITIVE)).completion, Completion.REFUSED)
        self.assertEqual(normalize_nvidia_response(nvidia_response(
            "length", refusal=SENSITIVE)).completion, Completion.REFUSED)
        self.assertEqual(normalize_bedrock_response(bedrock_response(
            "guardrail_intervened", extra=[{"toolUse": {}}])).completion, Completion.BLOCKED)

    def test_unknown_reason_is_preserved_without_guessing_from_text(self):
        for provider in RESPONSES:
            raw = (openai_response("future_reason") if provider == "openai"
                   else RESPONSES[provider]("future_reason"))
            result = NORMALIZERS[provider](raw)
            self.assertEqual(result.output_text, TEXT)
            self.assertEqual(result.provider_reason, "future_reason")
            self.assertEqual(result.completion, Completion.UNKNOWN)


class CompletionFlowTests(unittest.TestCase):
    def setUp(self):
        sessions.clear()
        self.client = TestClient(app)
        self.game_id = self.client.post("/api/games", json={}).json()["game_id"]
        self.url = f"/api/games/{self.game_id}/question"
        self.enterContext(patch.dict("os.environ", {
            "AI_STRUCTURED_OUTPUT_MODE": "off",
            "OPENAI_API_KEY": SENSITIVE, "OPENAI_MODEL_ID": "offline-model",
            "NVIDIA_API_KEY": SENSITIVE, "NVIDIA_MODEL_ID": "offline-model",
            "AWS_REGION": "us-east-1", "BEDROCK_MODEL_ID": "offline-model",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_QUALITY_MODE": "off", "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        }))
        sdk = self.enterContext(patch("define_ai_provider.OpenAI")).return_value
        sdk.timeout, sdk.max_retries = 12.0, 0
        aws = self.enterContext(patch("define_ai_provider.boto3.client")).return_value
        aws.meta.config = NS(connect_timeout=3, read_timeout=12,
                             retries={"mode": "standard", "total_max_attempts": 1})
        self.calls = {"openai": sdk.responses.create, "nvidia": sdk.chat.completions.create,
                      "bedrock": aws.converse}
        self.provider = "openai"
        self.enterContext(patch("question_generator.client", side_effect=lambda: LLMClient(self.provider)))
        self.sleep = self.enterContext(patch("api.sleep"))

    def reset_question(self, provider):
        self.provider = provider
        sessions[self.game_id].question = None
        sessions[self.game_id].question_id = None
        self.calls[provider].reset_mock(side_effect=True)
        self.sleep.reset_mock()

    def get_with_parse_spy(self):
        with self.assertLogs("quiz.diagnostics", level="INFO") as captured, \
                patch("question_generator.json.loads", wraps=json.loads) as parse:
            response = self.client.get(self.url)
        return response, parse, captured

    def test_all_provider_successes_parse_once_and_log_correlated_completion(self):
        for provider, reason in [("openai", "completed"), ("nvidia", "stop"), ("bedrock", "end_turn")]:
            with self.subTest(provider=provider):
                self.reset_question(provider)
                self.calls[provider].return_value = RESPONSES[provider]()
                response, parse, captured = self.get_with_parse_spy()
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("correct_answer", response.json())
                parse.assert_called_once()
                self.calls[provider].assert_called_once()
                events = [json.loads(record.getMessage()) for record in captured.records]
                event = next(e for e in events if e["event"] == "provider_completion")
                self.assertEqual(event["completion"], "complete")
                self.assertEqual(event["provider"], provider)
                self.assertEqual(event["provider_reason"], reason)
                self.assertEqual(event["attempt"], 1)
                self.assertEqual(event["request_id"], response.headers["X-Diagnostic-Request-ID"])
                for value in (SENSITIVE, QUESTION["question"], *QUESTION["answers"].values()):
                    self.assertNotIn(value, "\n".join(captured.output))

    def test_truncation_then_success_retries_without_parsing_truncated_json(self):
        failures = {"openai": openai_response("incomplete", "max_output_tokens"),
                    "nvidia": nvidia_response("length"), "bedrock": bedrock_response("max_tokens")}
        for provider, failure in failures.items():
            with self.subTest(provider=provider):
                self.reset_question(provider)
                categories = sessions[self.game_id].categories.copy()
                self.calls[provider].side_effect = [failure, RESPONSES[provider]()]
                response, parse, captured = self.get_with_parse_spy()
                self.assertEqual(response.status_code, 200)
                parse.assert_called_once()
                self.assertEqual(self.calls[provider].call_count, 2)
                self.sleep.assert_called_once()
                self.assertEqual(sessions[self.game_id].categories, categories[:-1])
                self.assertEqual(sessions[self.game_id].state.correct_main_answers, 0)
                events = [json.loads(r.getMessage()) for r in captured.records]
                self.assertEqual([e["completion"] for e in events if e["event"] == "provider_completion"],
                                 ["truncated", "complete"])

    def test_refusal_block_tool_and_unknown_fail_once_without_parse_or_state_change(self):
        cases = [("openai", openai_response(refusal=SENSITIVE)),
                 ("openai", openai_response("incomplete", "content_filter")),
                 ("openai", openai_response(extra=[NS(type="function_call", arguments=SENSITIVE)])),
                 ("openai", openai_response(status=None)),
                 ("nvidia", nvidia_response(refusal=SENSITIVE)),
                 ("nvidia", nvidia_response("content_filter")),
                 ("nvidia", nvidia_response("tool_calls", text=None)),
                 ("nvidia", nvidia_response(None)),
                 ("bedrock", bedrock_response("guardrail_intervened")),
                 ("bedrock", bedrock_response("content_filtered")),
                 ("bedrock", bedrock_response("tool_use", text=None)),
                 ("bedrock", bedrock_response(None))]
        for provider, raw in cases:
            with self.subTest(provider=provider, reason=NORMALIZERS[provider](raw).provider_reason):
                self.reset_question(provider)
                categories = sessions[self.game_id].categories.copy()
                self.calls[provider].return_value = raw
                response, parse, captured = self.get_with_parse_spy()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"detail": "AI provider unavailable"})
                self.calls[provider].assert_called_once()
                parse.assert_not_called()
                self.sleep.assert_not_called()
                self.assertEqual(sessions[self.game_id].categories, categories)
                self.assertEqual(sessions[self.game_id].state.correct_main_answers, 0)
                self.assertIsNone(sessions[self.game_id].question)
                self.assertNotIn(SENSITIVE, "\n".join(captured.output))
                self.assertFalse(any('question_validation' in r.getMessage() for r in captured.records))

    def test_openai_tool_items_fail_before_json_parsing(self):
        for kind in OPENAI_TOOL_OUTPUT_TYPES:
            for text in (None, TEXT):
                with self.subTest(kind=kind, with_text=text is not None):
                    self.reset_question("openai")
                    categories = sessions[self.game_id].categories.copy()
                    self.calls["openai"].return_value = openai_response(
                        text=text, extra=[NS(type=kind, arguments=SENSITIVE)])
                    response, parse, captured = self.get_with_parse_spy()
                    self.assertEqual(response.status_code, 503)
                    self.calls["openai"].assert_called_once()
                    parse.assert_not_called()
                    self.sleep.assert_not_called()
                    self.assertEqual(sessions[self.game_id].categories, categories)
                    self.assertIsNone(sessions[self.game_id].question)
                    events = [json.loads(r.getMessage()) for r in captured.records]
                    failure = next(e for e in events if e["event"] == "generation_attempt_completed")
                    self.assertEqual(failure["error_type"], "UnexpectedToolCallError")
                    self.assertFalse(failure["retryable"])
                    self.assertFalse(any(e["event"].startswith("question_validation") for e in events))
                    self.assertNotIn(SENSITIVE, "\n".join(captured.output))

    def test_completion_retry_exhaustion_obeys_shared_three_attempt_limit(self):
        self.provider = "nvidia"
        categories = sessions[self.game_id].categories.copy()
        self.calls["nvidia"].return_value = nvidia_response("length")
        response, parse, _ = self.get_with_parse_spy()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls["nvidia"].call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        parse.assert_not_called()
        self.assertEqual(sessions[self.game_id].categories, categories)

    def test_empty_completed_output_then_success_is_retryable_before_parsing(self):
        self.calls["openai"].side_effect = [openai_response(text=None), openai_response()]
        response, parse, _ = self.get_with_parse_spy()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls["openai"].call_count, 2)
        parse.assert_called_once()

    def test_completion_and_validation_failures_share_the_same_attempt_limit(self):
        invalid = json.dumps({**QUESTION, "difficulty": 8})
        self.calls["openai"].side_effect = [openai_response("incomplete", "max_output_tokens"),
                                               openai_response(text=invalid), openai_response()]
        response, parse, _ = self.get_with_parse_spy()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls["openai"].call_count, 3)
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(self.sleep.call_count, 2)

    def test_complete_fenced_json_remains_parseable_for_all_providers(self):
        for provider in RESPONSES:
            with self.subTest(provider=provider):
                self.reset_question(provider)
                fenced = "```json\n" + TEXT + "\n```"
                self.calls[provider].return_value = RESPONSES[provider](text=fenced)
                response, parse, _ = self.get_with_parse_spy()
                self.assertEqual(response.status_code, 200)
                parse.assert_called_once_with(TEXT)

    def test_completion_retries_obey_generation_budget(self):
        self.provider = "nvidia"
        clock = [100.0]
        def slow(**kwargs):
            clock[0] += 31
            return nvidia_response("length")
        self.calls["nvidia"].side_effect = slow
        with patch("api.perf_counter", side_effect=lambda: clock[0]):
            response, parse, _ = self.get_with_parse_spy()
        self.assertEqual(response.status_code, 503)
        self.calls["nvidia"].assert_called_once()
        parse.assert_not_called()
        self.sleep.assert_not_called()

    def test_tool_exception_is_typed_and_not_retryable(self):
        result = ProviderResult(TEXT, Completion.TOOL_CALL, "tool_calls")
        with self.assertRaises(UnexpectedToolCallError) as raised:
            completed_text(result)
        self.assertFalse(is_transient(raised.exception))


if __name__ == "__main__":
    unittest.main()
