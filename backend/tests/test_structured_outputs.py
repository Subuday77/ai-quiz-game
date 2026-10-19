"""Offline transport-schema, native payload, validation and no-downgrade tests."""

import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import botocore.session
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import validate_parameters
from fastapi.testclient import TestClient
from openai import APIStatusError
from pydantic import ValidationError

from api import app, sessions
from define_ai_provider import LLMClient
from generation_policy import is_transient
from output_contracts import OutputContract
from quality_schema import QUALITY_SCHEMA_NAME, quality_review_schema
from provider_result import Completion, ProviderResult
from question_schema import QuizQuestion
from question_transport_schema import QUIZ_SCHEMA_NAME, quiz_question_schema, quiz_question_schema_json
from structured_outputs import StructuredOutputConfigurationError, StructuredOutputSettings
from test_api import QUESTION
from test_provider_completion import (TEXT, bedrock_response, nvidia_response, openai_response)


SECRET = "private-key-prompt-question-model-error-do-not-log"
MODEL = "private-model-do-not-log"
FORMATS = [("openai", "guided_json"), ("nvidia", "guided_json"),
           ("nvidia", "json_schema"), ("bedrock", "guided_json")]
RESPONSES = {"openai": openai_response, "nvidia": nvidia_response, "bedrock": bedrock_response}


class SchemaTests(unittest.TestCase):
    def test_exact_shared_schema(self):
        self.assertEqual(quiz_question_schema(), {
            "type": "object", "properties": {
                "difficulty": {"type": "integer"}, "question": {"type": "string"},
                "answers": {"type": "object", "properties": {
                    "A": {"type": "string"}, "B": {"type": "string"},
                    "C": {"type": "string"}, "D": {"type": "string"}},
                    "required": ["A", "B", "C", "D"], "additionalProperties": False},
                "correct_answer": {"type": "string", "enum": ["A", "B", "C", "D"]}},
            "required": ["difficulty", "question", "answers", "correct_answer"],
            "additionalProperties": False,
        })
        self.assertEqual(QUIZ_SCHEMA_NAME, "quiz_question_v1")

    def test_only_portable_schema_keywords_are_present(self):
        def check(node):
            self.assertLessEqual(set(node), {"type", "properties", "required", "additionalProperties", "enum"})
            if node["type"] == "object":
                self.assertEqual(set(node["required"]), set(node["properties"]))
                self.assertIs(node["additionalProperties"], False)
                for child in node["properties"].values():
                    check(child)
        check(quiz_question_schema())
        for keyword in ("minimum", "maximum", "minLength", "maxLength", "minProperties",
                        "maxProperties", "propertyNames", "$ref", "$defs"):
            self.assertNotIn('"' + keyword + '"', quiz_question_schema_json())

    def test_transport_structure_matches_authoritative_pydantic_schema(self):
        native, app_schema = quiz_question_schema(), QuizQuestion.model_json_schema()
        self.assertEqual(set(native["properties"]), set(QuizQuestion.model_fields))
        self.assertEqual(set(native["required"]), set(app_schema["required"]))
        self.assertEqual(native["additionalProperties"], app_schema["additionalProperties"])
        for field in ("difficulty", "question", "answers", "correct_answer"):
            self.assertEqual(native["properties"][field]["type"], app_schema["properties"][field]["type"])
        choices = native["properties"]["answers"]["required"]
        self.assertEqual(choices, app_schema["properties"]["answers"]["propertyNames"]["enum"])
        self.assertEqual(choices, app_schema["properties"]["correct_answer"]["enum"])
        self.assertEqual(native["properties"]["correct_answer"]["enum"], choices)
        self.assertEqual(app_schema["properties"]["difficulty"]["minimum"], 0)
        self.assertEqual(app_schema["properties"]["difficulty"]["maximum"], 15)
        self.assertEqual(app_schema["properties"]["question"]["minLength"], 5)
        self.assertEqual(QuizQuestion.model_validate(QUESTION).model_dump(), QUESTION)

    def test_schema_is_stable_and_caller_mutation_cannot_poison_it(self):
        canonical = quiz_question_schema_json()
        schema = quiz_question_schema()
        schema["properties"]["answers"]["properties"]["A"]["type"] = "integer"
        schema["required"].clear()
        self.assertEqual(quiz_question_schema_json(), canonical)
        self.assertEqual(json.loads(canonical), quiz_question_schema())
        for difficulty in (0, 4, 15):
            QuizQuestion.model_validate({**QUESTION, "difficulty": difficulty})
            self.assertEqual(quiz_question_schema_json(), canonical)

    def test_pydantic_constraints_are_stricter_than_transport_constraints(self):
        for change in ({"difficulty": -1}, {"difficulty": 16}, {"question": ""},
                       {"answers": {"A": "x", "B": "x", "C": "y", "D": "z"}},
                       {"answers": {"A": " ", "B": "b", "C": "c", "D": "d"}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                QuizQuestion.model_validate({**QUESTION, **change})

    def test_default_off_and_explicit_format_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(StructuredOutputSettings.from_env("nvidia").mode, "off")
            with patch.dict("os.environ", {"AI_STRUCTURED_OUTPUT_MODE": "strict"}):
                self.assertEqual(StructuredOutputSettings.from_env("nvidia").output_format, "guided_json")
        with patch.dict("os.environ", {"AI_STRUCTURED_OUTPUT_MODE": "off",
                                       "NVIDIA_STRUCTURED_OUTPUT_FORMAT": "not-used"}):
            self.assertFalse(StructuredOutputSettings.from_env("nvidia").requested)

    def test_unknown_provider_is_rejected_in_strict_mode_without_retry(self):
        with patch.dict("os.environ", {"AI_STRUCTURED_OUTPUT_MODE": "strict"}):
            with self.assertRaises(StructuredOutputConfigurationError) as raised:
                StructuredOutputSettings.from_env("unsupported")
        self.assertEqual(raised.exception.reason_code, "unsupported_provider")
        self.assertFalse(is_transient(raised.exception))


class NativeRequestTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {
            "AI_STRUCTURED_OUTPUT_MODE": "off", "NVIDIA_STRUCTURED_OUTPUT_FORMAT": "guided_json",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_QUALITY_MODE": "off", "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
            "OPENAI_API_KEY": SECRET, "OPENAI_MODEL_ID": MODEL,
            "NVIDIA_API_KEY": SECRET, "NVIDIA_MODEL_ID": MODEL,
            "AWS_REGION": "us-east-1", "BEDROCK_MODEL_ID": MODEL,
        }))
        self.sdk = self.enterContext(patch("define_ai_provider.OpenAI")).return_value
        self.sdk.timeout, self.sdk.max_retries = 12.0, 0
        self.aws = self.enterContext(patch("define_ai_provider.boto3.client")).return_value
        self.aws.meta.config = NS(connect_timeout=3, read_timeout=12,
                                  retries={"mode": "standard", "total_max_attempts": 1})
        # Read installed SDK metadata locally; credentials and network are never consulted.
        self.aws.meta.service_model = botocore.session.get_session().get_service_model("bedrock-runtime")
        self.calls = {"openai": self.sdk.responses.create, "nvidia": self.sdk.chat.completions.create,
                      "bedrock": self.aws.converse}
        self.provider = "openai"
        self.enterContext(patch("question_generator.client", side_effect=lambda: LLMClient(self.provider)))
        self.sleep = self.enterContext(patch("api.sleep"))
        self.capture = self.enterContext(self.assertLogs("quiz.diagnostics", level="INFO"))
        sessions.clear()
        self.http = TestClient(app)
        self.game_id = self.http.post("/api/games", json={}).json()["game_id"]
        self.url = f"/api/games/{self.game_id}/question"

    def reset(self, provider, output_format="guided_json", mode="strict"):
        self.provider = provider
        self.enterContext(patch.dict("os.environ", {
            "AI_STRUCTURED_OUTPUT_MODE": mode, "NVIDIA_STRUCTURED_OUTPUT_FORMAT": output_format}))
        self.calls[provider].reset_mock(side_effect=True)
        self.calls[provider].return_value = RESPONSES[provider]()
        self.sleep.reset_mock()
        sessions[self.game_id].question = None
        sessions[self.game_id].question_id = None

    def direct(self, provider):
        self.calls[provider].return_value = RESPONSES[provider]()
        client = LLMClient(provider)
        return client.create(instructions=SECRET, input=SECRET, max_output_tokens=500,
                             reasoning={"effort": "none"}, temperature=1.0)

    def events(self):
        return [json.loads(record.getMessage()) for record in self.capture.records]

    def test_default_off_keeps_exact_native_request_parameters(self):
        for provider in self.calls:
            with self.subTest(provider=provider):
                result = self.direct(provider)
                self.assertIsInstance(result, ProviderResult)
                self.assertEqual(result.completion, Completion.COMPLETE)
                kwargs = self.calls[provider].call_args.kwargs
                if provider == "openai":
                    expected = {"model": MODEL, "instructions": SECRET, "input": SECRET,
                                "max_output_tokens": 500, "reasoning": {"effort": "none"}, "temperature": 1.0}
                elif provider == "nvidia":
                    expected = {"model": MODEL, "messages": [{"role": "system", "content": SECRET},
                                {"role": "user", "content": SECRET}], "max_completion_tokens": 500,
                                "temperature": 1.0, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
                else:
                    expected = {"modelId": MODEL, "system": [{"text": SECRET}],
                                "messages": [{"role": "user", "content": [{"text": SECRET}]}],
                                "inferenceConfig": {"maxTokens": 500, "temperature": 1.0}}
                self.assertEqual(kwargs, expected)
        self.assertTrue(all(not e["native_requested"] for e in self.events()
                            if e["event"] == "structured_output_request"))

    def test_openai_strict_payload(self):
        self.reset("openai")
        self.direct("openai")
        self.assertEqual(self.calls["openai"].call_args.kwargs["text"], {"format": {
            "type": "json_schema", "name": QUIZ_SCHEMA_NAME, "strict": True, "schema": quiz_question_schema()}})
        self.assertEqual(self.calls["openai"].call_args.kwargs["reasoning"], {"effort": "none"})
        self.assertEqual(self.calls["openai"].call_args.kwargs["max_output_tokens"], 500)

    def test_nvidia_guided_json_payload_preserves_chat_template(self):
        self.reset("nvidia", "guided_json")
        self.direct("nvidia")
        kwargs = self.calls["nvidia"].call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"chat_template_kwargs": {"enable_thinking": False},
                                                "guided_json": quiz_question_schema()})
        self.assertNotIn("response_format", kwargs)

    def test_nvidia_json_schema_payload_has_no_conflicting_guided_json(self):
        self.reset("nvidia", "json_schema")
        self.direct("nvidia")
        kwargs = self.calls["nvidia"].call_args.kwargs
        self.assertEqual(kwargs["response_format"], {"type": "json_schema", "json_schema": {
            "name": QUIZ_SCHEMA_NAME, "schema": quiz_question_schema()}})
        self.assertEqual(kwargs["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})

    def test_bedrock_payload_uses_json_string_and_no_undocumented_strict_flag(self):
        self.reset("bedrock")
        self.direct("bedrock")
        kwargs = self.calls["bedrock"].call_args.kwargs
        validate_parameters(kwargs, self.aws.meta.service_model.operation_model("Converse").input_shape)
        self.assertEqual(kwargs["outputConfig"], {"textFormat": {"type": "json_schema", "structure": {
            "jsonSchema": {"name": QUIZ_SCHEMA_NAME, "schema": quiz_question_schema_json()}}}})
        self.assertEqual(json.loads(kwargs["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"]),
                         quiz_question_schema())
        self.assertNotIn('"strict"', json.dumps(kwargs["outputConfig"]))

    def test_strict_success_correlates_diagnostics_and_keeps_answer_private(self):
        for provider, fmt in FORMATS:
            with self.subTest(provider=provider, fmt=fmt):
                self.reset(provider, fmt)
                response = self.http.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("correct_answer", response.json())
                events = [e for e in self.events() if e["request_id"] == response.headers["X-Diagnostic-Request-ID"]]
                request = next(e for e in events if e["event"] == "structured_output_request")
                result = next(e for e in events if e["event"] == "structured_output_result")
                self.assertTrue(request["native_requested"])
                self.assertEqual(request["provider"], provider)
                self.assertEqual(request["attempt"], 1)
                self.assertEqual(result["classification"], "complete")
                self.assertTrue(any(e["event"] == "question_validation_completed" for e in events))
                self.assertEqual(sessions[self.game_id].state.correct_main_answers, 0)

    def test_schema_is_identical_across_requested_difficulty_levels(self):
        for provider, fmt in FORMATS:
            schemas = []
            for level in (0, 6, 15):
                self.reset(provider, fmt)
                sessions[self.game_id].state.correct_main_answers = level if level < 15 else 15
                sessions[self.game_id].state.phase = "main" if level < 15 else "bonus"
                self.calls[provider].return_value = RESPONSES[provider](text=json.dumps({**QUESTION, "difficulty": level}))
                self.assertEqual(self.http.get(self.url).status_code, 200)
                kwargs = self.calls[provider].call_args.kwargs
                if provider == "openai":
                    schema = kwargs["text"]["format"]["schema"]
                elif provider == "nvidia":
                    schema = (kwargs["extra_body"]["guided_json"] if fmt == "guided_json"
                              else kwargs["response_format"]["json_schema"]["schema"])
                else:
                    schema = json.loads(kwargs["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"])
                schemas.append(schema)
            self.assertTrue(all(schema == quiz_question_schema() for schema in schemas))

    def test_schema_400_and_422_do_not_downgrade_or_repeat(self):
        for provider, fmt in FORMATS:
            for status in (400, 422):
                with self.subTest(provider=provider, fmt=fmt, status=status):
                    self.reset(provider, fmt)
                    if provider == "bedrock":
                        exc = ClientError({"Error": {"Code": "ValidationException", "Message": SECRET},
                                           "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")
                    else:
                        exc = APIStatusError(SECRET, body={"schema": quiz_question_schema()},
                                             response=Mock(status_code=status, headers={}, request=Mock()))
                    self.calls[provider].side_effect = exc
                    categories = sessions[self.game_id].categories.copy()
                    with patch("question_generator.json.loads", wraps=json.loads) as parse:
                        response = self.http.get(self.url)
                    self.assertEqual(response.status_code, 503)
                    self.calls[provider].assert_called_once()
                    parse.assert_not_called()
                    self.sleep.assert_not_called()
                    self.assertEqual(sessions[self.game_id].categories, categories)
                    self.assertIsNone(sessions[self.game_id].question)
                    self.assertEqual(sessions[self.game_id].state.correct_main_answers, 0)
                    failures = [e for e in self.events() if e["event"] == "structured_output_failed"]
                    self.assertEqual(failures[-1]["classification"], "provider_request_rejected")

    def test_invalid_config_is_nonretryable_and_never_calls_provider(self):
        self.reset("nvidia")
        cases = [{"AI_STRUCTURED_OUTPUT_MODE": SECRET}, {"NVIDIA_STRUCTURED_OUTPUT_FORMAT": SECRET}]
        for env in cases:
            with self.subTest(env_key=next(iter(env))), patch.dict("os.environ", env):
                self.assertEqual(self.http.get(self.url).status_code, 503)
        self.calls["nvidia"].assert_not_called()
        self.sleep.assert_not_called()
        self.assertIsNone(sessions[self.game_id].question)
        self.assertNotIn(SECRET, "\n".join(self.capture.output))

    def test_old_bedrock_sdk_is_rejected_locally_and_off_mode_still_works(self):
        self.reset("bedrock")
        self.aws.meta.service_model = NS(operation_model=lambda _: NS(input_shape=NS(members={})))
        self.assertEqual(self.http.get(self.url).status_code, 503)
        self.calls["bedrock"].assert_not_called()
        self.sleep.assert_not_called()
        self.assertTrue(any(e.get("reason_code") == "bedrock_sdk_unsupported" for e in self.events()))
        with patch.dict("os.environ", {"AI_STRUCTURED_OUTPUT_MODE": "off"}):
            self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertNotIn("outputConfig", self.calls["bedrock"].call_args.kwargs)

    def test_unsupported_sdk_parameters_are_not_retried(self):
        for provider, exc in [("openai", TypeError(SECRET)),
                              ("bedrock", ParamValidationError(report=SECRET))]:
            with self.subTest(provider=provider):
                self.reset(provider)
                self.calls[provider].side_effect = exc
                self.assertEqual(self.http.get(self.url).status_code, 503)
                self.calls[provider].assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual([e for e in self.events() if e["event"] == "structured_output_failed"][-1]
                                 ["classification"], "sdk_request_rejected")

    def test_strict_completion_gate_precedes_json_and_pydantic(self):
        failures = [("openai", openai_response(refusal=SECRET)),
                    ("openai", openai_response("incomplete", "content_filter")),
                    ("openai", openai_response(extra=[NS(type="program", code=SECRET)])),
                    ("nvidia", nvidia_response("tool_calls")),
                    ("nvidia", nvidia_response("content_filter")),
                    ("nvidia", nvidia_response(None)),
                    ("bedrock", bedrock_response("guardrail_intervened")),
                    ("bedrock", bedrock_response("tool_use")),
                    ("bedrock", bedrock_response(None))]
        for provider, raw in failures:
            with self.subTest(provider=provider):
                self.reset(provider)
                self.calls[provider].return_value = raw
                with patch("question_generator.json.loads", wraps=json.loads) as parse, \
                        patch("api.QuizQuestion.model_validate") as validate:
                    self.assertEqual(self.http.get(self.url).status_code, 503)
                parse.assert_not_called()
                validate.assert_not_called()
                self.calls[provider].assert_called_once()
                self.sleep.assert_not_called()

    def test_strict_truncation_still_uses_shared_bounded_retry_loop(self):
        for provider, failure in [("openai", openai_response("incomplete", "max_output_tokens")),
                                   ("nvidia", nvidia_response("length")),
                                   ("bedrock", bedrock_response("max_tokens"))]:
            with self.subTest(provider=provider):
                self.reset(provider)
                self.calls[provider].side_effect = [failure, RESPONSES[provider]()]
                with patch("question_generator.json.loads", wraps=json.loads) as parse:
                    self.assertEqual(self.http.get(self.url).status_code, 200)
                self.assertEqual(self.calls[provider].call_count, 2)
                parse.assert_called_once()
                self.sleep.assert_called_once()

    def test_strict_schema_claim_does_not_bypass_pydantic_or_difficulty_check(self):
        invalid = [
            {**QUESTION, "difficulty": 16}, {**QUESTION, "difficulty": 1},
            {**QUESTION, "question": "x"}, {**QUESTION, "extra": "forbidden"},
            {**QUESTION, "correct_answer": "X"}, {**QUESTION, "answers": {"A": "only one"}},
            {**QUESTION, "answers": {"A": "a", "B": "A ", "C": "c", "D": "d"}},
            {**QUESTION, "answers": {"A": "a", "B": " ", "C": "c", "D": "d"}},
        ]
        for provider, fmt in FORMATS:
            for data in invalid:
                with self.subTest(provider=provider, fmt=fmt, data=data):
                    self.reset(provider, fmt)
                    self.calls[provider].return_value = RESPONSES[provider](text=json.dumps(data))
                    categories = sessions[self.game_id].categories.copy()
                    self.assertEqual(self.http.get(self.url).status_code, 502)
                    self.assertEqual(self.calls[provider].call_count, 3)
                    self.assertEqual(self.sleep.call_count, 2)
                    self.assertEqual(sessions[self.game_id].categories, categories)
                    self.assertIsNone(sessions[self.game_id].question)
                    self.assertEqual(sessions[self.game_id].state.current_difficulty, 0)

    def test_strict_invalid_json_then_valid_response_keeps_all_validation_steps(self):
        for provider, fmt in FORMATS:
            with self.subTest(provider=provider, fmt=fmt):
                self.reset(provider, fmt)
                self.calls[provider].side_effect = [RESPONSES[provider](text="not json"), RESPONSES[provider]()]
                self.assertEqual(self.http.get(self.url).status_code, 200)
                self.assertEqual(self.calls[provider].call_count, 2)

    def test_strict_transient_provider_error_still_retries_without_downgrade(self):
        self.reset("nvidia", "json_schema")
        exc = APIStatusError(SECRET, body=None, response=Mock(status_code=429, headers={}, request=Mock()))
        self.calls["nvidia"].side_effect = [exc, nvidia_response()]
        self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(self.calls["nvidia"].call_count, 2)
        self.assertTrue(all("response_format" in call.kwargs for call in self.calls["nvidia"].call_args_list))

    def test_diagnostics_do_not_log_credentials_text_schema_or_raw_error(self):
        self.reset("openai")
        self.direct("openai")
        self.calls["openai"].side_effect = APIStatusError(
            SECRET + quiz_question_schema_json(), body={"schema": quiz_question_schema()},
            response=Mock(status_code=400, headers={}, request=Mock()))
        with self.assertRaises(APIStatusError):
            self.direct("openai")
        output = "\n".join(self.capture.output)
        for value in (SECRET, MODEL, QUESTION["question"], *QUESTION["answers"].values(),
                      quiz_question_schema_json(), "additionalProperties", "chat_template_kwargs"):
            self.assertNotIn(value, output)
        self.assertTrue(all(record.exc_info is None for record in self.capture.records))

    def test_review_contract_selects_distinct_native_schema_for_all_formats(self):
        for provider, output_format in FORMATS:
            with self.subTest(provider=provider, output_format=output_format):
                self.reset(provider, output_format)
                returned = LLMClient(provider).create(
                    instructions=SECRET, input=SECRET, max_output_tokens=500,
                    output_contract=OutputContract.QUALITY_REVIEW)
                self.assertEqual(returned.completion, Completion.COMPLETE)
                kwargs = self.calls[provider].call_args.kwargs
                self.assertNotIn("output_contract", kwargs)
                if provider == "openai":
                    native = kwargs["text"]["format"]
                    self.assertTrue(native["strict"])
                elif provider == "nvidia" and output_format == "guided_json":
                    self.assertEqual(kwargs["extra_body"]["guided_json"], quality_review_schema())
                    self.assertEqual(kwargs["extra_body"]["chat_template_kwargs"], {"enable_thinking": False})
                    self.assertNotIn("response_format", kwargs)
                    continue
                elif provider == "nvidia":
                    native = kwargs["response_format"]["json_schema"]
                    self.assertNotIn("guided_json", kwargs["extra_body"])
                else:
                    validate_parameters(kwargs, self.aws.meta.service_model.operation_model("Converse").input_shape)
                    native = kwargs["outputConfig"]["textFormat"]["structure"]["jsonSchema"]
                    native = {**native, "schema": json.loads(native["schema"])}
                self.assertEqual(native["name"], QUALITY_SCHEMA_NAME)
                self.assertEqual(native["schema"], quality_review_schema())
        self.assertTrue(all(e["output_contract"] == "quality_review" for e in self.events()
                            if e["event"] == "structured_output_request"))

    def test_review_off_contract_keeps_native_request_parameters(self):
        for provider in self.calls:
            with self.subTest(provider=provider):
                self.reset(provider, mode="off")
                self.direct(provider)
                previous = self.calls[provider].call_args.kwargs
                LLMClient(provider).create(instructions=SECRET, input=SECRET, max_output_tokens=500,
                    reasoning={"effort": "none"}, temperature=1.0,
                    output_contract=OutputContract.QUALITY_REVIEW)
                self.assertEqual(self.calls[provider].call_args.kwargs, previous)

    def test_invalid_contract_is_permanent_and_does_not_call_provider(self):
        self.reset("openai")
        with self.assertRaises(StructuredOutputConfigurationError) as error:
            LLMClient("openai").create(instructions=SECRET, input=SECRET, output_contract="future_task")
        self.assertFalse(is_transient(error.exception))
        self.calls["openai"].assert_not_called()

    def test_strict_generator_and_review_pipeline_uses_separate_contracts(self):
        verdict = {"decision": "accept", "issue_codes": [], "independent_answer": "B", "rationale": SECRET}
        for provider, fmt in FORMATS:
            with self.subTest(provider=provider, fmt=fmt):
                self.reset(provider, fmt)
                self.calls[provider].side_effect = [RESPONSES[provider](),
                    RESPONSES[provider](text=json.dumps(verdict))]
                with patch.dict("os.environ", {"AI_QUALITY_MODE": "enforce"}), patch(
                        "quality_checker.client", side_effect=lambda: LLMClient(provider)):
                    response = self.http.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.calls[provider].call_count, 2)
                self.assertEqual([e["output_contract"] for e in self.events()
                                  if e["event"] == "structured_output_request"][-2:],
                                 ["question", "quality_review"])
                self.assertEqual(self.sdk.max_retries, 0)
                self.assertNotIn(SECRET, " ".join(self.capture.output))


    def test_strict_review_parameter_rejection_never_downgrades_or_retries(self):
        for provider, fmt in FORMATS:
            for status in (400, 422):
                with self.subTest(provider=provider, fmt=fmt, status=status):
                    self.reset(provider, fmt)
                    error = (ClientError({"Error": {"Code": "ValidationException", "Message": SECRET},
                             "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")
                             if provider == "bedrock" else APIStatusError(SECRET, body=None,
                                 response=Mock(status_code=status, headers={}, request=Mock())))
                    self.calls[provider].side_effect = [RESPONSES[provider](), error]
                    categories = sessions[self.game_id].categories.copy()
                    with patch.dict("os.environ", {"AI_QUALITY_MODE": "enforce"}), patch(
                            "quality_checker.client", side_effect=lambda: LLMClient(provider)):
                        self.assertEqual(self.http.get(self.url).status_code, 503)
                    self.assertEqual(self.calls[provider].call_count, 2)
                    self.sleep.assert_not_called()
                    self.assertIsNone(sessions[self.game_id].question)
                    self.assertEqual(sessions[self.game_id].categories, categories)
                    self.assertNotIn(SECRET, " ".join(self.capture.output))

    def test_review_remaining_budget_is_applied_to_sdk_socket_configuration(self):
        import define_ai_provider
        for provider in self.calls:
            with self.subTest(provider=provider):
                self.reset(provider, mode="off")
                verdict = {"decision": "accept", "issue_codes": [], "independent_answer": "B", "rationale": SECRET}
                self.calls[provider].return_value = RESPONSES[provider](text=json.dumps(verdict))
                now = [0.0]
                def generate(*args):
                    now[0] = 28.0
                    return QUESTION
                with patch.dict("os.environ", {"AI_QUALITY_MODE": "enforce"}), patch(
                        "api.perf_counter", side_effect=lambda: now[0]), patch(
                        "api.create_question", side_effect=generate), patch(
                        "quality_checker.client", side_effect=lambda: LLMClient(provider)):
                    self.assertEqual(self.http.get(self.url).status_code, 200)
                if provider == "bedrock":
                    config = define_ai_provider.boto3.client.call_args.kwargs["config"]
                    self.assertEqual(config.read_timeout, 2.0)
                    self.assertEqual(config.connect_timeout, 2.0)
                    self.assertEqual(config.retries["total_max_attempts"], 1)
                else:
                    config = define_ai_provider.OpenAI.call_args.kwargs
                    self.assertEqual(config["timeout"], 2.0)
                    self.assertEqual(config["max_retries"], 0)



if __name__ == "__main__":
    unittest.main()
