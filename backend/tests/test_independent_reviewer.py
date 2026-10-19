"""Offline role/model selection and multilingual fixtures, not model-accuracy tests."""

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import botocore.session
from fastapi.testclient import TestClient

from api import app, sessions
from define_ai_provider import LLMClient, PROVIDER
from output_contracts import OutputContract
from quality_checker import lexical_findings
from quality_configuration import ReviewerSettings
from quality_errors import QualityConfigurationError
from quality_schema import IssueCode
from question_schema import QuizQuestion
from structured_outputs import StructuredOutputConfigurationError
from test_api import QUESTION
from test_provider_completion import openai_response, nvidia_response, bedrock_response

SUPER = "nvidia/nemotron-3-super-120b-a12b"
NANO = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
SECRET = "private-prompt-key-rationale-model-value"
ACCEPT = {"decision": "accept", "issue_codes": [], "independent_answer": "B", "rationale": SECRET}
FIXTURES = json.loads((Path(__file__).parent / "fixtures/quality_examples.json").read_text(encoding="utf-8"))


class ReviewerSettingsTests(unittest.TestCase):
    def test_inheritance_and_explicit_settings_do_not_mutate_environment(self):
        with patch.dict(os.environ, {"AI_STRUCTURED_OUTPUT_MODE": "strict"}, clear=True):
            before = dict(os.environ)
            settings = ReviewerSettings.from_env()
            self.assertEqual((settings.provider, settings.model_id, settings.structured_output_mode), (PROVIDER, None, "strict"))
            self.assertEqual(dict(os.environ), before)
        for provider in ("nvidia", "openai", "bedrock"):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                    "AI_QUALITY_PROVIDER": provider, "AI_QUALITY_MODEL_ID": "custom-reviewer",
                    "AI_STRUCTURED_OUTPUT_MODE": "strict", "AI_QUALITY_STRUCTURED_OUTPUT_MODE": "off"}, clear=True):
                before = dict(os.environ)
                settings = ReviewerSettings.from_env()
                self.assertEqual((settings.provider, settings.model_id, settings.structured_output_mode), (provider, "custom-reviewer", "off"))
                self.assertEqual(dict(os.environ), before)

    def test_partial_blank_and_invalid_configuration_is_explicit(self):
        for values in ({"AI_QUALITY_PROVIDER": "nvidia"}, {"AI_QUALITY_MODEL_ID": NANO},
                       {"AI_QUALITY_PROVIDER": "", "AI_QUALITY_MODEL_ID": NANO},
                       {"AI_QUALITY_PROVIDER": "other", "AI_QUALITY_MODEL_ID": NANO},
                       {"AI_QUALITY_PROVIDER": "nvidia", "AI_QUALITY_MODEL_ID": " "}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), self.assertRaises(QualityConfigurationError):
                ReviewerSettings.from_env()
        for mode in ("", "STRICT", "invalid"):
            with patch.dict(os.environ, {"AI_QUALITY_STRUCTURED_OUTPUT_MODE": mode}, clear=True), self.assertRaises(StructuredOutputConfigurationError):
                ReviewerSettings.from_env()


class IndependentReviewerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
        for key in ("AI_QUALITY_PROVIDER", "AI_QUALITY_MODEL_ID", "AI_QUALITY_STRUCTURED_OUTPUT_MODE"):
            os.environ.pop(key, None)
        self.enterContext(patch.dict(os.environ, {
            "AI_QUALITY_MODE": "enforce", "AI_STRUCTURED_OUTPUT_MODE": "off",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2", "NVIDIA_STRUCTURED_OUTPUT_FORMAT": "guided_json",
            "NVIDIA_API_KEY": SECRET, "NVIDIA_MODEL_ID": SUPER,
            "OPENAI_API_KEY": SECRET, "OPENAI_MODEL_ID": "generator-openai-model",
            "AWS_REGION": "us-east-1", "BEDROCK_MODEL_ID": "generator-bedrock-model"}))
        self.sdk = Mock(timeout=12.0, max_retries=0)
        self.aws = Mock()
        self.aws.meta.config = NS(connect_timeout=3.0, read_timeout=12.0,
                                  retries={"total_max_attempts": 1, "mode": "standard"})
        self.aws.meta.service_model = botocore.session.get_session().get_service_model("bedrock-runtime")
        self.enterContext(patch("define_ai_provider.OpenAI", return_value=self.sdk))
        self.enterContext(patch("define_ai_provider.boto3.client", return_value=self.aws))
        self.calls = {"openai": self.sdk.responses.create, "nvidia": self.sdk.chat.completions.create,
                      "bedrock": self.aws.converse}
        self.raw = {"openai": openai_response, "nvidia": nvidia_response, "bedrock": bedrock_response}
        for provider, call in self.calls.items():
            call.return_value = self.raw[provider](text=json.dumps(ACCEPT))
        self.factory = self.enterContext(patch("quality_checker.LLMClient", wraps=LLMClient))
        self.enterContext(patch("api.sleep"))
        self.logs = self.enterContext(self.assertLogs("quiz.diagnostics", level="INFO"))
        sessions.clear()
        self.http = TestClient(app)
        self.game = self.http.post("/api/games", json={}).json()["game_id"]
        self.session = sessions[self.game]
        self.url = f"/api/games/{self.game}/question"

    def events(self, name):
        return [json.loads(r.getMessage()) for r in self.logs.records if json.loads(r.getMessage())["event"] == name]

    def test_model_override_reaches_each_sdk_without_default_model_environment(self):
        for provider, env_name in (("nvidia", "NVIDIA_MODEL_ID"), ("openai", "OPENAI_MODEL_ID"), ("bedrock", "BEDROCK_MODEL_ID")):
            with self.subTest(provider=provider), patch.dict(os.environ):
                os.environ.pop(env_name, None)
                LLMClient(provider, model_id="explicit-review-model", role="reviewer").create(
                    instructions=SECRET, input=SECRET, output_contract=OutputContract.QUALITY_REVIEW)
                kwargs = self.calls[provider].call_args.kwargs
                self.assertEqual(kwargs["modelId" if provider == "bedrock" else "model"], "explicit-review-model")
        text = " ".join(self.logs.output)
        self.assertNotIn("explicit-review-model", text)
        self.assertNotIn(SECRET, text)

    def test_super_generator_nano_reviewer_with_independent_schema_modes(self):
        self.sdk.chat.completions.create.side_effect = [nvidia_response(text=json.dumps(QUESTION)),
                                                        nvidia_response(text=json.dumps(ACCEPT))]
        with patch.dict(os.environ, {"AI_STRUCTURED_OUTPUT_MODE": "strict", "AI_QUALITY_PROVIDER": "nvidia",
                "AI_QUALITY_MODEL_ID": NANO, "AI_QUALITY_STRUCTURED_OUTPUT_MODE": "off"}), patch(
                "question_generator.client", side_effect=lambda: LLMClient("nvidia")):
            before = dict(os.environ)
            response = self.http.get(self.url)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(response.status_code, 200)
        generated, reviewed = [call.kwargs for call in self.sdk.chat.completions.create.call_args_list]
        self.assertEqual((generated["model"], reviewed["model"]), (SUPER, NANO))
        self.assertIn("guided_json", generated["extra_body"])
        self.assertEqual(reviewed["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertNotIn("response_format", reviewed)
        self.assertLessEqual(reviewed["timeout"], 12.0)
        requests = self.events("structured_output_request")
        self.assertEqual([(e["role"], e["model_id"], e["structured_output_mode"]) for e in requests],
                         [("generator", SUPER, "strict"), ("reviewer", NANO, "off")])
        self.assertEqual({e["request_id"] for e in requests}, {response.headers["X-Diagnostic-Request-ID"]})
        self.assertEqual({e["attempt"] for e in requests}, {1})
        self.assertNotIn(SECRET, " ".join(self.logs.output))

    def test_cross_provider_reviewer_does_not_change_generator(self):
        self.sdk.chat.completions.create.return_value = nvidia_response(text=json.dumps(QUESTION))
        with patch.dict(os.environ, {"AI_QUALITY_PROVIDER": "openai", "AI_QUALITY_MODEL_ID": "review-openai",
                "AI_QUALITY_STRUCTURED_OUTPUT_MODE": "strict", "AI_STRUCTURED_OUTPUT_MODE": "off"}), patch(
                "question_generator.client", side_effect=lambda: LLMClient("nvidia")):
            self.assertEqual(self.http.get(self.url).status_code, 200)
        generated = self.sdk.chat.completions.create.call_args.kwargs
        reviewed = self.sdk.responses.create.call_args.kwargs
        self.assertEqual(generated["model"], SUPER)
        self.assertNotIn("guided_json", generated["extra_body"])
        self.assertEqual(reviewed["model"], "review-openai")
        self.assertEqual(reviewed["text"]["format"]["name"], "quality_review_v1")
        self.assertEqual([(e["role"], e["provider"]) for e in self.events("structured_output_request")],
                         [("generator", "nvidia"), ("reviewer", "openai")])

    def test_reviewer_strict_and_generator_off_payloads_for_all_providers(self):
        for provider in self.calls:
            with self.subTest(provider=provider):
                generator = LLMClient(provider)
                generator.create(instructions=SECRET, input=SECRET)
                before = self.calls[provider].call_args.kwargs
                reviewer = LLMClient(provider, model_id="review-model", structured_output_mode="strict", role="reviewer")
                reviewer.create(instructions=SECRET, input=SECRET, output_contract=OutputContract.QUALITY_REVIEW)
                kwargs = self.calls[provider].call_args.kwargs
                if provider == "openai":
                    self.assertEqual(kwargs["text"]["format"]["name"], "quality_review_v1")
                elif provider == "nvidia":
                    self.assertIn("decision", kwargs["extra_body"]["guided_json"]["properties"])
                else:
                    self.assertEqual(kwargs["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["name"], "quality_review_v1")
                generator.create(instructions=SECRET, input=SECRET)
                self.assertEqual(self.calls[provider].call_args.kwargs, before)

    def test_missing_and_invalid_settings_respect_audit_enforce_without_review_sdk(self):
        invalid = [{"AI_QUALITY_PROVIDER": "nvidia"}, {"AI_QUALITY_MODEL_ID": NANO},
                   {"AI_QUALITY_PROVIDER": "unknown", "AI_QUALITY_MODEL_ID": NANO},
                   {"AI_QUALITY_STRUCTURED_OUTPUT_MODE": "invalid"}]
        for settings in invalid:
            for mode, expected in (("audit", 200), ("enforce", 503)):
                with self.subTest(settings=settings, mode=mode), patch.dict(os.environ, {**settings, "AI_QUALITY_MODE": mode}), patch(
                        "api.create_question", return_value=QUESTION):
                    self.session.question = None
                    self.session.question_id = None
                    categories = list(self.session.categories)
                    self.assertEqual(self.http.get(self.url).status_code, expected)
                    if mode == "enforce":
                        self.assertIsNone(self.session.question)
                        self.assertEqual(self.session.categories, categories)
        self.factory.assert_not_called()
        for call in self.calls.values():
            call.assert_not_called()
        self.assertEqual({e["classification"] for e in self.events("quality_review_failed")}, {"configuration_error"})

    def test_off_never_resolves_reviewer_settings_or_constructs_reviewer(self):
        with patch.dict(os.environ, {"AI_QUALITY_MODE": "off", "AI_QUALITY_PROVIDER": "invalid"}), patch(
                "quality_checker.ReviewerSettings.from_env", side_effect=AssertionError("Must not initialize reviewer")), patch(
                "api.create_question", return_value=QUESTION):
            self.assertEqual(self.http.get(self.url).status_code, 200)
        self.factory.assert_not_called()

    def test_no_overrides_inherits_generator_model_and_schema_mode(self):
        with patch.dict(os.environ, {"AI_STRUCTURED_OUTPUT_MODE": "strict"}), patch(
                "api.create_question", return_value=QUESTION):
            self.assertEqual(self.http.get(self.url).status_code, 200)
        self.factory.assert_called_once_with(PROVIDER, model_id=None, structured_output_mode="strict", role="reviewer")
        request = self.events("structured_output_request")[0]
        self.assertEqual(request["provider"], PROVIDER)
        self.assertEqual(request["structured_output_mode"], "strict")

    def test_first_channel_literal_rejection_uses_no_reviewer_and_preserves_state(self):
        example = next(x for x in FIXTURES["leakage"] if x["language"] == "Russian")
        candidate = {**QUESTION, "question": example["leaked"], "correct_answer": "A",
                     "answers": {"A": example["answer"], "B": "Channel two", "C": "Channel three", "D": "Channel four"}}
        categories = list(self.session.categories)
        with patch("api.create_question", return_value=candidate) as generated:
            self.assertEqual(self.http.get(self.url).status_code, 502)
        self.assertEqual(generated.call_count, 3)
        self.factory.assert_not_called()
        self.assertIsNone(self.session.question)
        self.assertEqual(self.session.categories, categories)
        self.assertEqual(self.session.state.correct_main_answers, 0)
        self.assertEqual({e["source"] for e in self.events("quality_question_rejected")}, {"local_precheck"})

    def test_mocked_real_example_verdicts_preserve_state_and_semantics(self):
        # These verdicts test policy handling; they do not measure live reviewer quality.
        for example in FIXTURES["judgments"]:
            with self.subTest(name=example["name"]), patch.dict(os.environ, {
                    "AI_QUALITY_PROVIDER": "nvidia", "AI_QUALITY_MODEL_ID": NANO}):
                candidate = example["question"]
                self.session.language = example["language"]
                self.session.state.correct_main_answers = candidate["difficulty"]
                self.session.question = None
                self.session.question_id = None
                categories = list(self.session.categories)
                verdict = {"decision": "regenerate" if example["issues"] else "accept",
                           "issue_codes": example["issues"], "independent_answer": "A", "rationale": SECRET}
                self.assertEqual(lexical_findings(QuizQuestion.model_validate(candidate)), [])
                self.sdk.chat.completions.create.return_value = nvidia_response(text=json.dumps(verdict))
                with patch("api.create_question", return_value=candidate):
                    response = self.http.get(self.url)
                self.assertEqual(response.status_code, 502 if example["issues"] else 200)
                self.assertEqual(self.session.state.correct_main_answers, candidate["difficulty"])
                if example["issues"]:
                    self.assertIsNone(self.session.question)
                    self.assertEqual(self.session.categories, categories)
                else:
                    self.assertEqual(len(self.session.categories), len(categories) - 1)
                self.assertNotIn("correct_answer", response.json())


class MultilingualLeakageTests(unittest.TestCase):
    def test_positive_and_negative_full_answer_matches_for_all_seven_languages(self):
        self.assertEqual({x["language"] for x in FIXTURES["leakage"]},
                         {"English", "Russian", "Hebrew", "German", "French", "Spanish", "Italian"})
        for example in FIXTURES["leakage"]:
            for leaked in (True, False):
                with self.subTest(language=example["language"], leaked=leaked):
                    candidate = {**QUESTION, "question": example["leaked" if leaked else "clean"],
                                 "answers": {"A": example["answer"], "B": "Other one", "C": "Other two", "D": "Other three"},
                                 "correct_answer": "A"}
                    self.assertEqual(lexical_findings(QuizQuestion.model_validate(candidate)),
                                     [IssueCode.ANSWER_LEAK] if leaked else [])

    def test_normalization_boundaries_and_necessary_shared_words(self):
        for question, answer, expected in (
                ('Who is named "BIG\u00a0BEN"?', 'Big Ben', True),
                ('What does "tea" refer to here?', 'Tea', True),
                ('What is a teapot made from?', 'Tea', False),
                ('What surrounds a red giant?', 'Red Sea', False),
                ('Who was born in New Jersey?', 'New York', False),
                ('How many wheels does a bicycle have?', 'Two', False),
                ('Which planet is closest to the Sun?', 'Mercury', False)):
            with self.subTest(question=question):
                candidate = {**QUESTION, "question": question, "correct_answer": "A",
                             "answers": {"A": answer, "B": "Option two", "C": "Option three", "D": "Option four"}}
                self.assertEqual(lexical_findings(QuizQuestion.model_validate(candidate)), [IssueCode.ANSWER_LEAK] if expected else [])

    def test_mentioning_several_options_does_not_deterministically_single_out_key(self):
        candidate = {**QUESTION, "question": 'Is Mercury or Venus closer to the Sun?',
                     "answers": {"A": "Mercury", "B": "Venus", "C": "Earth", "D": "Mars"}, "correct_answer": "A"}
        self.assertEqual(lexical_findings(QuizQuestion.model_validate(candidate)), [])
