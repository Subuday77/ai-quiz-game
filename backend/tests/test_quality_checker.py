"""Offline quality-policy integration; every generator and reviewer call is mocked."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from openai import APIStatusError
from pydantic import ValidationError

from api import app, sessions
from generation_policy import is_transient, provider_timeout
from diagnostics import generation_attempt, request_id
from define_ai_provider import LLMClient
from test_provider_completion import openai_response, nvidia_response, bedrock_response
from output_contracts import OutputContract
from provider_result import Completion, ProviderResult
from quality_checker import check_quality, lexical_findings, quality_mode_from_env
from quality_errors import QualityConfigurationError, QualityReviewFailure
from quality_schema import (IssueCode, QualityMode, QualityReview, ReviewDecision,
                            quality_review_schema)
from question_schema import QuizQuestion
from test_api import QUESTION

SECRET = "private-rationale-credential-provider-exception"
ACCEPT = {"decision": "accept", "issue_codes": [], "independent_answer": "B", "rationale": SECRET}
REGENERATE = {**ACCEPT, "decision": "regenerate", "issue_codes": ["ambiguous"]}
ESCALATE = {**ACCEPT, "decision": "escalate", "issue_codes": ["needs_verification"]}


def result(verdict=ACCEPT, completion=Completion.COMPLETE):
    return ProviderResult(json.dumps(verdict), completion, "test_reason")


class VerdictTests(unittest.TestCase):
    def test_transport_and_pydantic_contract_match(self):
        schema = quality_review_schema()
        self.assertEqual(set(schema["properties"]), set(QualityReview.model_fields))
        self.assertEqual(schema["required"], list(QualityReview.model_fields))
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["decision"]["enum"], [d.value for d in ReviewDecision])
        self.assertEqual(schema["properties"]["issue_codes"]["items"]["enum"], [i.value for i in IssueCode])
        self.assertEqual(schema["properties"]["independent_answer"]["enum"], ["A", "B", "C", "D", "unknown"])
        self.assertEqual(schema["properties"]["rationale"], {"type": "string"})
        schema["required"].clear()
        self.assertEqual(len(quality_review_schema()["required"]), 4)

    def test_invalid_verdicts_are_not_approval(self):
        for change in ({"decision": "yes"}, {"issue_codes": ["invented"]},
                       {"extra": "field"}, {"rationale": " "}, {"rationale": "x" * 601},
                       {"independent_answer": "unknown"}, {"issue_codes": ["answer_leak"]},
                       {"decision": "regenerate"}, {"independent_answer": "E"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                QualityReview.model_validate({**ACCEPT, **change})
        for verdict in (ACCEPT, REGENERATE, ESCALATE):
            QualityReview.model_validate(verdict)
        with self.assertRaises(ValidationError):
            QualityReview.model_validate({**REGENERATE, "issue_codes": ["ambiguous", "ambiguous"]})

    def test_default_and_explicit_mode_validation(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(quality_mode_from_env(), QualityMode.OFF)
        for mode in QualityMode:
            with patch.dict("os.environ", {"AI_QUALITY_MODE": mode.value}):
                self.assertEqual(quality_mode_from_env(), mode)
        for invalid in ("", "AUDIT", "invalid"):
            with patch.dict("os.environ", {"AI_QUALITY_MODE": invalid}), self.assertRaises(QualityConfigurationError):
                quality_mode_from_env()

    def test_conservative_unicode_leakage_and_negative_cases(self):
        # Unicode escapes preserve this fixture under Windows console encodings.
        word = "\u0410\u043b\u0435\u043a\u0441\u0430\u043d\u0434\u0440\u043e\u0432\u0441\u043a\u0438\u0439"
        candidate = {**QUESTION, "question": 'Who is called "' + word + '"?',
                     "answers": {**QUESTION["answers"], "B": word}}
        self.assertEqual(lexical_findings(QuizQuestion.model_validate(candidate)), [IssueCode.ANSWER_LEAK])
        # A one-character discrepancy in a long distinctive name is also flagged.
        candidate["answers"]["B"] = word[:-1] + "x"
        self.assertEqual(lexical_findings(QuizQuestion.model_validate(candidate)), [IssueCode.ANSWER_LEAK])
        for question in (QUESTION, {**candidate, "question": 'Where did ' + word + ' live?'},
                         {**QUESTION, "question": 'What does "planet" mean?'},
                         {**candidate, "answers": {**candidate["answers"], "A": word + " city"}}):
            self.assertEqual(lexical_findings(QuizQuestion.model_validate(question)), [])


class QualityApiTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ"))
        for key in ("AI_QUALITY_PROVIDER", "AI_QUALITY_MODEL_ID", "AI_QUALITY_STRUCTURED_OUTPUT_MODE"):
            os.environ.pop(key, None)
        sessions.clear()
        self.enterContext(patch.dict("os.environ", {
            "AI_QUALITY_MODE": "enforce", "AI_STRUCTURED_OUTPUT_MODE": "off",
            "AI_GENERATION_MAX_ATTEMPTS": "3", "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2", "LLM_PROVIDER": "nvidia",
            "NVIDIA_API_KEY": SECRET}))
        self.generated = self.enterContext(patch("api.create_question", return_value=QUESTION))
        self.factory = self.enterContext(patch("quality_checker.LLMClient"))
        self.review = self.factory.return_value.responses.create
        self.review.return_value = result()
        self.sleep = self.enterContext(patch("api.sleep"))
        self.logs = self.enterContext(self.assertLogs("quiz.diagnostics", level="INFO"))
        self.http = TestClient(app)
        self.game = self.http.post("/api/games", json={}).json()["game_id"]
        self.session = sessions[self.game]
        self.initial_categories = list(self.session.categories)
        self.url = f"/api/games/{self.game}/question"

    def events(self, name):
        return [json.loads(record.getMessage()) for record in self.logs.records
                if json.loads(record.getMessage())["event"] == name]

    def unchanged(self, difficulty=0):
        self.assertIsNone(self.session.question)
        self.assertIsNone(self.session.question_id)
        self.assertEqual(self.session.categories, self.initial_categories)
        self.assertEqual(self.session.state.correct_main_answers, difficulty)
        self.assertEqual(self.session.state.phase, "main")
        self.assertIsNone(self.session.state.bonus_correct)

    def test_off_has_no_review_calls_and_preserves_response(self):
        with patch.dict("os.environ", {"AI_QUALITY_MODE": "off"}):
            response = self.http.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.factory.assert_not_called()
        self.assertEqual(set(response.json()), {"question_id", "number", "difficulty", "question", "answers", "bonus"})
        self.assertEqual(self.events("quality_review_skipped")[0]["reason"], "disabled")

    def test_enforce_accept_and_cached_retry_after_lost_browser_response(self):
        first = self.http.get(self.url)  # Treat this response as lost by the browser.
        second = self.http.get(self.url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.generated.assert_called_once()
        self.review.assert_called_once()
        kwargs = self.review.call_args.kwargs
        self.assertEqual(kwargs["output_contract"], OutputContract.QUALITY_REVIEW)
        self.assertIn("untrusted data", kwargs["instructions"])
        self.assertEqual(json.loads(kwargs["input"])["candidate"], QUESTION)
        self.assertNotIn("correct_answer", second.json())
        self.assertNotIn("rationale", second.json())

    def test_regeneration_shares_three_attempt_limit(self):
        self.review.return_value = result(REGENERATE)
        response = self.http.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.generated.call_count, 3)
        self.assertEqual(self.review.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual([e["attempt"] for e in self.events("quality_question_rejected")], [1, 2, 3])
        self.unchanged()

    def test_rejected_candidates_then_accept(self):
        self.review.side_effect = [result(REGENERATE), result(REGENERATE), result()]
        self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(self.generated.call_count, 3)
        self.assertEqual(self.review.call_count, 3)
        self.assertEqual(len(self.session.categories), len(self.initial_categories) - 1)
        self.assertEqual(self.session.state.correct_main_answers, 0)

    def test_escalation_does_not_retry(self):
        self.review.return_value = result(ESCALATE)
        self.assertEqual(self.http.get(self.url).status_code, 503)
        self.review.assert_called_once()
        self.generated.assert_called_once()
        self.sleep.assert_not_called()
        self.unchanged()

    def test_disagreeing_accept_is_escalated(self):
        self.review.return_value = result({**ACCEPT, "independent_answer": "A"})
        self.assertEqual(self.http.get(self.url).status_code, 503)
        self.assertEqual(self.events("quality_review_completed")[0]["decision"], "escalate")
        self.review.assert_called_once()
        self.unchanged()

    def test_audit_keeps_candidate_for_every_verdict(self):
        for verdict in (ACCEPT, REGENERATE, ESCALATE):
            with self.subTest(verdict=verdict), patch.dict("os.environ", {"AI_QUALITY_MODE": "audit"}):
                self.session.question = None
                self.review.return_value = result(verdict)
                self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(self.generated.call_count, 3)
        self.assertEqual(self.review.call_count, 3)

    def test_audit_failure_does_not_break_gameplay(self):
        for failure in (TimeoutError(SECRET), ValueError(SECRET)):
            with self.subTest(failure=type(failure)), patch.dict("os.environ", {"AI_QUALITY_MODE": "audit"}):
                self.session.question = None
                self.review.side_effect = failure
                self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(self.generated.call_count, 2)
        self.assertEqual(len(self.events("quality_review_failed")), 2)

    def test_validation_and_difficulty_failures_do_not_call_reviewer(self):
        for candidate in ({**QUESTION, "correct_answer": "E"}, {**QUESTION, "difficulty": 1}):
            with self.subTest(candidate=candidate):
                self.generated.return_value = candidate
                self.assertEqual(self.http.get(self.url).status_code, 502)
        self.factory.assert_not_called()
        self.unchanged()

    def test_generator_json_failure_does_not_call_reviewer(self):
        self.generated.side_effect = json.JSONDecodeError(SECRET, "", 0)
        self.assertEqual(self.http.get(self.url).status_code, 502)
        self.factory.assert_not_called()
        self.unchanged()

    def test_invalid_mode_fails_before_generation(self):
        with patch.dict("os.environ", {"AI_QUALITY_MODE": SECRET}):
            self.assertEqual(self.http.get(self.url).status_code, 503)
        self.generated.assert_not_called()
        self.factory.assert_not_called()
        self.unchanged()

    def test_review_completion_is_checked_before_json_parsing(self):
        for completion in (Completion.TRUNCATED, Completion.REFUSED, Completion.BLOCKED,
                           Completion.TOOL_CALL, Completion.UNKNOWN):
            with self.subTest(completion=completion), patch("quality_checker.json.loads") as parse:
                self.review.reset_mock()
                self.review.return_value = result(completion=completion)
                self.assertEqual(self.http.get(self.url).status_code, 503)
                parse.assert_not_called()
                self.assertEqual(self.review.call_count, 3 if completion == Completion.TRUNCATED else 1)
                self.unchanged()

    def test_malformed_json_and_invalid_schema_fail_closed_without_retry(self):
        for text in ("not json " + SECRET, json.dumps({**ACCEPT, "unexpected": SECRET}),
                     json.dumps({**ACCEPT, "rationale": ""})):
            with self.subTest(text=text):
                self.review.reset_mock()
                self.review.return_value = ProviderResult(text, Completion.COMPLETE, "stop")
                self.assertEqual(self.http.get(self.url).status_code, 503)
                self.review.assert_called_once()
                self.unchanged()
        self.assertTrue(all(e["classification"] == "invalid_verdict" for e in self.events("quality_review_failed")))

    def test_transient_review_failures_retry_whole_attempt_only(self):
        self.review.side_effect = [TimeoutError(SECRET), ConnectionError(SECRET), result()]
        self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(self.generated.call_count, 3)
        self.assertEqual(self.review.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_transient_review_exhaustion_preserves_state(self):
        self.review.side_effect = TimeoutError(SECRET)
        self.assertEqual(self.http.get(self.url).status_code, 503)
        self.assertEqual(self.review.call_count, 3)
        self.assertEqual(self.generated.call_count, 3)
        self.unchanged()

    def test_unsupported_schema_parameters_never_downgrade_or_retry(self):
        for status in (400, 422):
            with self.subTest(status=status):
                self.review.reset_mock()
                response = httpx.Response(status, request=httpx.Request("POST", "https://invalid.example"))
                self.review.side_effect = APIStatusError(SECRET, response=response, body={"secret": SECRET})
                self.assertEqual(self.http.get(self.url).status_code, 503)
                self.review.assert_called_once()
                self.unchanged()

    def test_retry_after_remains_bounded(self):
        response = httpx.Response(429, headers={"retry-after": "10"},
                                  request=httpx.Request("POST", "https://invalid.example"))
        self.review.side_effect = APIStatusError(SECRET, response=response, body=None)
        self.assertEqual(self.http.get(self.url).status_code, 503)
        self.review.assert_called_once()
        self.sleep.assert_not_called()
        self.unchanged()

    def test_remaining_budget_limits_reviewer_timeout(self):
        now = [0.0]
        def generate(*args):
            now[0] = 28.0
            return QUESTION
        observed = []
        self.generated.side_effect = generate
        self.factory.side_effect = lambda *args, **kwargs: (observed.append(provider_timeout.get()) or Mock(
            responses=Mock(create=Mock(return_value=result()))))
        with patch("api.perf_counter", side_effect=lambda: now[0]):
            self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(observed, [2.0])
        self.assertIsNone(provider_timeout.get())

    def test_budget_exhaustion_before_review_audit_vs_enforce(self):
        for mode, status in (("audit", 200), ("enforce", 503)):
            now = [0.0]
            self.session.question = None
            self.session.question_id = None
            def generate(*args):
                now[0] = 30.0
                return QUESTION
            self.generated.side_effect = generate
            with self.subTest(mode=mode), patch.dict("os.environ", {"AI_QUALITY_MODE": mode}), patch(
                    "api.perf_counter", side_effect=lambda: now[0]):
                self.assertEqual(self.http.get(self.url).status_code, status)
        self.factory.assert_not_called()
        self.assertEqual(len(self.events("quality_review_skipped")), 2)

    def test_client_setup_cannot_start_review_after_budget_expiry(self):
        now = [0.0]
        def factory(*args, **kwargs):
            now[0] = 31.0
            return Mock(responses=Mock(create=self.review))
        self.factory.side_effect = factory
        with patch("api.perf_counter", side_effect=lambda: now[0]):
            self.assertEqual(self.http.get(self.url).status_code, 503)
        self.review.assert_not_called()
        self.unchanged()

    def test_deterministic_rejection_is_separate_from_model_review(self):
        self.generated.return_value = {**QUESTION, "question": 'Who is called "Alexandrovsky"?',
                                      "answers": {**QUESTION["answers"], "B": "Alexandrovsky"}}
        self.assertEqual(self.http.get(self.url).status_code, 502)
        self.factory.assert_not_called()
        self.assertEqual({e["source"] for e in self.events("quality_question_rejected")}, {"local_precheck"})
        self.unchanged()

    def test_historical_regression_mocked_review_blocks_without_progress(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/historical_nickname.json").read_text(encoding="utf-8"))
        self.session.state.correct_main_answers = 5
        self.generated.return_value = fixture
        self.review.return_value = result({"decision": "regenerate", "issue_codes": ["factual_error", "answer_leak", "needs_verification"],
            "independent_answer": "A", "rationale": "Wrong historical nickname; causal explanation unsupported; lexical giveaway."})
        # The conservative local heuristic intentionally does not claim to catch this subtle pair.
        self.assertEqual(lexical_findings(QuizQuestion.model_validate(fixture)), [])
        self.assertEqual(self.http.get(self.url).status_code, 502)
        self.assertEqual(self.review.call_count, 3)
        self.unchanged(difficulty=5)
        response = self.http.post(f"/api/games/{self.game}/answer", json={"question_id": "not-issued", "answer": "A"})
        self.assertEqual(response.status_code, 409)
        self.unchanged(difficulty=5)

    def test_concurrent_requests_share_generation_review_and_reward_once(self):
        entered, release = Event(), Event()
        self.session.state.correct_main_answers = 4
        self.generated.return_value = {**QUESTION, "difficulty": 4}
        def review(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release reviewer")
            return result()
        self.review.side_effect = review
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.http.get, self.url)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.http.get, self.url)
            finally:
                release.set()
            one, two = first.result(5), second.result(5)
            self.assertEqual(one.status_code, 200)
            self.assertEqual(one.json(), two.json())
            self.generated.assert_called_once()
            self.review.assert_called_once()
            payload = {"question_id": one.json()["question_id"], "answer": "B"}
            url = f"/api/games/{self.game}/answer"
            answers = list(pool.map(lambda _: self.http.post(url, json=payload), range(2)))
        self.assertEqual(sorted(a.status_code for a in answers), [200, 409])
        self.assertEqual([a.json().get("achievement") for a in answers].count("bronze"), 1)
        self.assertEqual(self.session.state.correct_main_answers, 5)

    def test_diagnostics_redact_question_key_rationale_and_exceptions(self):
        self.review.side_effect = [TimeoutError(SECRET), result(REGENERATE), result()]
        response = self.http.get(self.url)
        self.assertEqual(response.status_code, 200)
        text = " ".join(r.getMessage() for r in self.logs.records)
        for sensitive in (SECRET, QUESTION["question"], *QUESTION["answers"].values(),
                          "correct_answer", "independent_answer", "rationale"):
            self.assertNotIn(sensitive, text)
        for name in ("quality_review_started", "quality_review_completed", "quality_review_failed", "quality_question_rejected"):
            self.assertTrue(self.events(name))
            for entry in self.events(name):
                self.assertEqual(entry["request_id"], response.headers["X-Diagnostic-Request-ID"])
                self.assertIn(entry["attempt"], (1, 2, 3))

    def test_audit_malformed_and_safety_failure_still_returns_candidate(self):
        for review in (ProviderResult("bad JSON", Completion.COMPLETE, "stop"),
                       result(completion=Completion.REFUSED),
                       result({**ACCEPT, "extra": "forbidden"})):
            self.session.question = None
            self.review.return_value = review
            with self.subTest(review=review.completion), patch.dict("os.environ", {"AI_QUALITY_MODE": "audit"}):
                self.assertEqual(self.http.get(self.url).status_code, 200)
        self.assertEqual(self.review.call_count, 3)
        self.sleep.assert_not_called()

    def test_audit_local_findings_are_reported_without_blocking(self):
        self.generated.return_value = {**QUESTION, "question": 'Who is called "Alexandrovsky"?',
                                      "answers": {**QUESTION["answers"], "B": "Alexandrovsky"}}
        with patch.dict("os.environ", {"AI_QUALITY_MODE": "audit"}):
            self.assertEqual(self.http.get(self.url).status_code, 200)
        self.review.assert_called_once()
        self.assertEqual(self.events("quality_review_completed")[0]["local_issue_codes"], ["answer_leak"])
        self.assertFalse(self.events("quality_question_rejected"))


class ReviewerTimeoutTests(unittest.TestCase):
    """Exercise the real provider wrapper; every SDK constructor/request is mocked."""

    def setUp(self):
        self.enterContext(patch.dict("os.environ"))
        for key in ("AI_QUALITY_PROVIDER", "AI_QUALITY_MODEL_ID", "AI_QUALITY_STRUCTURED_OUTPUT_MODE"):
            os.environ.pop(key, None)
        self.enterContext(patch.dict("os.environ", {
            "AI_STRUCTURED_OUTPUT_MODE": "off", "OPENAI_API_KEY": SECRET,
            "OPENAI_MODEL_ID": "offline", "NVIDIA_API_KEY": SECRET,
            "NVIDIA_MODEL_ID": "offline", "AWS_REGION": "us-east-1",
            "BEDROCK_MODEL_ID": "offline"}))
        self.sdk = Mock(max_retries=0)
        self.aws = Mock()
        self.openai_constructor = self.enterContext(patch("define_ai_provider.OpenAI"))
        self.aws_constructor = self.enterContext(patch("define_ai_provider.boto3.client"))
        self.factory = self.enterContext(patch("quality_checker.LLMClient"))
        self.now = 0.0
        self.init_seconds = 0.0
        def openai_constructor(**kwargs):
            self.sdk.timeout = kwargs["timeout"]
            self.now += self.init_seconds
            return self.sdk
        def aws_constructor(*args, **kwargs):
            self.aws.meta.config = kwargs["config"]
            self.now += self.init_seconds
            return self.aws
        self.openai_constructor.side_effect = openai_constructor
        self.aws_constructor.side_effect = aws_constructor
        self.calls = {"openai": self.sdk.responses.create,
                      "nvidia": self.sdk.chat.completions.create,
                      "bedrock": self.aws.converse}
        self.raw = {"openai": openai_response, "nvidia": nvidia_response, "bedrock": bedrock_response}
        self.logs = self.enterContext(self.assertLogs("quiz.diagnostics", level="INFO"))
        self.context_tokens = (provider_timeout.set(7.0), generation_attempt.set(2), request_id.set("timeout-regression"))
        self.addCleanup(request_id.reset, self.context_tokens[2])
        self.addCleanup(generation_attempt.reset, self.context_tokens[1])
        self.addCleanup(provider_timeout.reset, self.context_tokens[0])

    def reset(self, provider, initialization):
        self.now = 0.0
        self.init_seconds = initialization
        self.factory.side_effect = lambda *args, **kwargs: LLMClient(provider, **kwargs)
        for call in self.calls.values():
            call.reset_mock(side_effect=True)
        self.calls[provider].return_value = self.raw[provider](text=json.dumps(ACCEPT))

    def review(self, mode=QualityMode.ENFORCE):
        try:
            return check_quality(QuizQuestion.model_validate(QUESTION), 0, "English", mode,
                                 deadline=30.0, timeout_seconds=12.0, clock=lambda: self.now)
        finally:
            self.assertEqual(provider_timeout.get(), 7.0)
            self.assertEqual(generation_attempt.get(), 2)
            self.assertEqual(request_id.get(), "timeout-regression")

    def test_slow_initialization_uses_openai_and_nvidia_request_override(self):
        for provider in ("openai", "nvidia"):
            for mode in (QualityMode.AUDIT, QualityMode.ENFORCE):
                with self.subTest(provider=provider, mode=mode):
                    self.reset(provider, initialization=27.0)
                    self.review(mode)
                    self.calls[provider].assert_called_once()
                    self.assertEqual(self.sdk.timeout, 12.0)  # Construction snapshot is stale.
                    self.assertEqual(self.calls[provider].call_args.kwargs["timeout"], 3.0)
                    self.assertEqual(self.openai_constructor.call_args.kwargs["max_retries"], 0)
                    self.assertNotIn("request_timeout", self.calls[provider].call_args.kwargs)

    def test_slow_bedrock_initialization_safely_withholds_unbounded_review(self):
        for mode in (QualityMode.AUDIT, QualityMode.ENFORCE):
            with self.subTest(mode=mode):
                self.reset("bedrock", initialization=27.0)
                if mode == QualityMode.AUDIT:
                    self.review(mode)
                else:
                    with self.assertRaises(QualityReviewFailure) as raised:
                        self.review(mode)
                    self.assertEqual(raised.exception.classification, "timeout_allocation_unavailable")
                    self.assertFalse(is_transient(raised.exception))
                self.aws.converse.assert_not_called()
                self.assertEqual(self.aws.meta.config.read_timeout, 12.0)
                self.assertEqual(self.aws_constructor.call_count, 1 if mode == QualityMode.AUDIT else 2)
                # No client reconstruction or transport-internal mutation.
                self.assertEqual(self.aws.meta.config.retries["total_max_attempts"], 1)

    def test_initialization_exhausts_budget_for_every_provider_and_mode(self):
        for provider in self.calls:
            for mode in (QualityMode.AUDIT, QualityMode.ENFORCE):
                with self.subTest(provider=provider, mode=mode):
                    self.reset(provider, initialization=30.0)
                    if mode == QualityMode.AUDIT:
                        self.review(mode)
                    else:
                        with self.assertRaises(QualityReviewFailure) as raised:
                            self.review(mode)
                        self.assertEqual(raised.exception.classification, "budget_exhausted")
                    self.calls[provider].assert_not_called()

    def test_bedrock_uses_existing_socket_limits_only_when_both_fit(self):
        self.reset("bedrock", initialization=10.0)
        self.review()
        self.aws.converse.assert_called_once()
        kwargs = self.aws.converse.call_args.kwargs
        self.assertNotIn("timeout", kwargs)
        self.assertNotIn("request_timeout", kwargs)
        self.assertEqual(self.aws.meta.config.read_timeout, 12.0)
        self.assertEqual(self.aws.meta.config.connect_timeout, 3.0)
        entries = [json.loads(record.getMessage()) for record in self.logs.records]
        allocated = next(e for e in entries if e["event"] == "provider_timeout_allocated")
        self.assertEqual(allocated["allocation_seconds"], 12.0)
        self.assertEqual(allocated["read_timeout_seconds"], 12.0)
        self.assertEqual(allocated["application"], "existing_socket_limits")

    def test_allocation_is_recomputed_after_schema_setup_immediately_at_dispatch(self):
        from structured_outputs import StructuredOutputSettings
        original = StructuredOutputSettings.request_parameters
        def prepare(settings, *args, **kwargs):
            params = original(settings, *args, **kwargs)
            self.now += 18.0
            return params
        for provider in ("openai", "nvidia"):
            with self.subTest(provider=provider):
                self.reset(provider, initialization=10.0)
                with patch.object(StructuredOutputSettings, "request_parameters", prepare):
                    self.review()
                self.assertEqual(self.calls[provider].call_args.kwargs["timeout"], 2.0)

    def test_budget_exhausted_during_schema_setup_never_dispatches(self):
        from structured_outputs import StructuredOutputSettings
        original = StructuredOutputSettings.request_parameters
        def prepare(settings, *args, **kwargs):
            params = original(settings, *args, **kwargs)
            self.now = 30.0
            return params
        for provider in self.calls:
            for mode in (QualityMode.AUDIT, QualityMode.ENFORCE):
                with self.subTest(provider=provider, mode=mode):
                    self.reset(provider, initialization=10.0)
                    with patch.object(StructuredOutputSettings, "request_parameters", prepare):
                        if mode == QualityMode.AUDIT:
                            self.review(mode)
                        else:
                            with self.assertRaises(QualityReviewFailure) as raised:
                                self.review(mode)
                            self.assertEqual(raised.exception.classification, "budget_exhausted")
                    self.calls[provider].assert_not_called()

    def test_request_failure_restores_context_and_preserves_audit_enforce_policy(self):
        for provider in self.calls:
            for mode in (QualityMode.AUDIT, QualityMode.ENFORCE):
                with self.subTest(provider=provider, mode=mode):
                    self.reset(provider, initialization=1.0)
                    self.calls[provider].side_effect = TimeoutError(SECRET)
                    if mode == QualityMode.AUDIT:
                        self.review(mode)
                    else:
                        with self.assertRaises(QualityReviewFailure) as raised:
                            self.review(mode)
                        self.assertTrue(is_transient(raised.exception))
                    self.calls[provider].assert_called_once()

    def test_initialization_failure_restores_context(self):
        for mode in (QualityMode.AUDIT, QualityMode.ENFORCE):
            with self.subTest(mode=mode):
                self.reset("openai", initialization=0.0)
                self.openai_constructor.side_effect = ValueError(SECRET)
                if mode == QualityMode.AUDIT:
                    self.review(mode)
                else:
                    with self.assertRaises(QualityReviewFailure) as raised:
                        self.review(mode)
                    self.assertFalse(is_transient(raised.exception))
                self.sdk.responses.create.assert_not_called()
