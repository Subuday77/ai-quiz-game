"""Diagnostic integration tests: provider SDK calls and clocks are mocked."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app, sessions
from define_ai_provider import LLMClient
from diagnostics import request_id, generation_attempt
from test_api import QUESTION


SECRET = "fake-secret-do-not-log"


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        sessions.clear()
        self.enterContext(patch("api.sleep"))
        self.client = TestClient(app)
        self.game_id = self.client.post("/api/games", json={}).json()["game_id"]
        self.url = f"/api/games/{self.game_id}/question"
        self.enterContext(patch.dict("os.environ", {
            "AI_GENERATION_MAX_ATTEMPTS": "3", "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
            "OPENAI_API_KEY": SECRET, "OPENAI_MODEL_ID": "offline-model",
            "NVIDIA_API_KEY": SECRET, "NVIDIA_MODEL_ID": "offline-model",
            "AWS_REGION": "us-east-1", "BEDROCK_MODEL_ID": "offline-model",
        }))
        # Constructors cannot reach a real SDK or credential discovery.
        self.openai = self.enterContext(patch("define_ai_provider.OpenAI"))
        self.boto = self.enterContext(patch("define_ai_provider.boto3.client"))
        self.sdk = self.openai.return_value
        self.sdk.timeout = SimpleNamespace(connect=12, read=12, write=12, pool=12)
        self.sdk.max_retries = 0
        self.sdk_call = self.sdk.chat.completions.create
        self.factory = self.enterContext(patch("question_generator.client",
                                              side_effect=lambda: LLMClient("nvidia")))

    def response(self, data):
        text = json.dumps(data) if isinstance(data, dict) else data
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(
                content=text, refusal=None, tool_calls=None, function_call=None))])

    def capture(self):
        return self.assertLogs("quiz.diagnostics", level="INFO")

    def events(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def selected(self, events, name):
        return [entry for entry in events if entry["event"] == name]

    def assert_safe(self, captured):
        output = "\n".join(captured.output)
        for value in [SECRET, QUESTION["question"], *QUESTION["answers"].values(),
                      "Generate a question with difficulty", "Authorization"]:
            self.assertNotIn(value, output)
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_success_events_have_correlation_and_separate_durations(self):
        clock = [10.0]

        def provider_call(**kwargs):
            clock[0] += 3.0
            return self.response(QUESTION)

        original_loads = json.loads

        def parse(text):
            clock[0] += 0.2
            return original_loads(text)

        self.sdk_call.side_effect = provider_call
        with self.capture() as captured, \
                patch("diagnostics.perf_counter", side_effect=lambda: clock[0]), \
                patch("api.perf_counter", side_effect=lambda: clock[0]), \
                patch("question_generator.json.loads", side_effect=parse):
            result = self.client.get(self.url)
        events = self.events(captured)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()), {
            "question_id", "number", "difficulty", "question", "answers", "bonus"})
        correlation = result.headers["X-Diagnostic-Request-ID"]
        self.assertTrue(all(e["request_id"] == correlation for e in events))
        names = [e["event"] for e in events]
        for name in ["question_request_received", "session_lock_wait_started",
                     "session_lock_acquired", "question_cache", "generation_attempt_started",
                     "provider_client_setup_started", "provider_configuration",
                     "provider_client_setup_completed", "provider_request_started",
                     "provider_request_completed", "question_json_parse_started",
                     "question_json_parse_completed", "question_validation_started",
                     "question_validation_completed", "generation_attempt_completed",
                     "question_request_completed"]:
            self.assertIn(name, names)
        self.assertEqual(self.selected(events, "provider_request_completed")[0]["duration_ms"], 3000)
        self.assertEqual(self.selected(events, "question_json_parse_completed")[0]["duration_ms"], 200)
        self.assertEqual(self.selected(events, "question_validation_completed")[0]["duration_ms"], 0)
        self.assertEqual(events[-1]["duration_ms"], 3200)
        self.assertEqual(events[-1]["outcome"], "success")
        self.assertEqual(self.selected(events, "provider_request_started")[0]["attempt"], 1)
        self.assert_safe(captured)
        self.assertIsNone(request_id.get())
        self.assertIsNone(generation_attempt.get())

    def test_schema_failure_then_success_logs_two_attempts_without_content(self):
        self.sdk_call.side_effect = [self.response({**QUESTION, "correct_answer": SECRET}),
                                     self.response(QUESTION)]
        with self.capture() as captured:
            result = self.client.get(self.url)
        events = self.events(captured)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([e["attempt"] for e in self.selected(events, "provider_request_started")], [1, 2])
        self.assertEqual([e["outcome"] for e in self.selected(events, "generation_attempt_completed")],
                         ["invalid_question", "success"])
        self.assertEqual(len(self.selected(events, "question_validation_failed")), 1)
        self.assertEqual(len(self.selected(events, "question_validation_completed")), 1)
        self.assert_safe(captured)

    def test_invalid_json_retries_and_logs_parsing_failure(self):
        self.sdk_call.side_effect = [self.response(SECRET), self.response(QUESTION)]
        with self.capture() as captured:
            result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200)
        events = self.events(captured)
        failed = self.selected(events, "question_json_parse_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_type"], "JSONDecodeError")
        self.assertIn("duration_ms", failed[0])
        self.assert_safe(captured)

    def test_provider_exception_is_safe_and_not_retried_by_application(self):
        self.sdk_call.side_effect = RuntimeError(SECRET + QUESTION["question"])
        with self.capture() as captured:
            result = self.client.get(self.url)
        events = self.events(captured)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json(), {"detail": "AI provider unavailable"})
        self.sdk_call.assert_called_once()
        failure = self.selected(events, "provider_request_failed")[0]
        self.assertEqual(failure["error_type"], "RuntimeError")
        self.assertGreaterEqual(failure["duration_ms"], 0)
        self.assertFalse(self.selected(events, "question_json_parse_started"))
        self.assertEqual(events[-1]["outcome"], "failure")
        self.assert_safe(captured)

    def test_openai_and_bedrock_exceptions_have_provider_failure_events(self):
        self.sdk.responses.create.side_effect = RuntimeError(SECRET)
        bedrock = self.boto.return_value
        bedrock.meta.config = SimpleNamespace(connect_timeout=3, read_timeout=12,
                                             retries={"mode": "legacy"})
        bedrock.converse.side_effect = RuntimeError(SECRET)
        for provider in ("openai", "bedrock"):
            self.factory.side_effect = lambda: LLMClient(provider)
            with self.subTest(provider=provider), self.capture() as captured:
                result = self.client.get(self.url)
            self.assertEqual(result.status_code, 503)
            failures = self.selected(self.events(captured), "provider_request_failed")
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["provider"], provider)
            self.assert_safe(captured)
        self.sdk.responses.create.assert_called_once()
        bedrock.converse.assert_called_once()

    def test_client_setup_exception_is_measured_without_provider_call(self):
        self.openai.side_effect = RuntimeError(SECRET)
        with self.capture() as captured:
            result = self.client.get(self.url)
        self.assertEqual(result.status_code, 503)
        events = self.events(captured)
        self.assertEqual(len(self.selected(events, "provider_client_setup_failed")), 1)
        self.assertFalse(self.selected(events, "provider_request_started"))
        self.assert_safe(captured)

    def test_exhausted_validation_has_three_attempts_and_502(self):
        self.sdk_call.return_value = self.response({**QUESTION, "difficulty": 9})
        with self.capture() as captured:
            result = self.client.get(self.url)
        events = self.events(captured)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json(), {"detail": "AI could not generate a valid question"})
        self.assertEqual([e["attempt"] for e in self.selected(events, "generation_attempt_started")], [1, 2, 3])
        self.assertEqual(len(self.selected(events, "question_validation_failed")), 3)
        self.assertEqual(events[-1]["status_code"], 502)
        self.assertIsNone(sessions[self.game_id].question)
        self.assert_safe(captured)

    def test_cache_does_not_invoke_provider_and_request_ids_are_unique(self):
        self.sdk_call.return_value = self.response(QUESTION)
        with self.capture():
            first = self.client.get(self.url)
        self.sdk_call.reset_mock()
        self.factory.reset_mock()
        with self.capture() as captured:
            cached = self.client.get(self.url)
        events = self.events(captured)
        self.assertEqual(first.json(), cached.json())
        self.assertNotEqual(first.headers["X-Diagnostic-Request-ID"],
                            cached.headers["X-Diagnostic-Request-ID"])
        self.sdk_call.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.selected(events, "question_cache")[0]["hit"], True)
        self.assertFalse(self.selected(events, "generation_attempt_started"))
        self.assertFalse(self.selected(events, "provider_request_started"))

    def test_lock_wait_is_measured_separately_from_provider(self):
        waiting = Event()
        clock = [10.0]
        real_lock = sessions[self.game_id].lock
        real_lock.acquire()

        class ObservedLock:
            def __enter__(self):
                waiting.set()
                real_lock.acquire()

            def __exit__(self, *args):
                real_lock.release()

        sessions[self.game_id].lock = ObservedLock()
        self.sdk_call.return_value = self.response(QUESTION)
        with self.capture() as captured, \
                patch("api.perf_counter", side_effect=lambda: clock[0]), \
                patch("diagnostics.perf_counter", side_effect=lambda: clock[0]), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.get, self.url)
            try:
                self.assertTrue(waiting.wait(timeout=5))
                self.assertFalse(future.done())
                clock[0] += 2.0
            finally:
                real_lock.release()
            self.assertEqual(future.result(timeout=5).status_code, 200)
        events = self.events(captured)
        self.assertEqual(self.selected(events, "session_lock_acquired")[0]["duration_ms"], 2000)
        self.assertEqual(self.selected(events, "provider_request_completed")[0]["duration_ms"], 0)

    def test_unknown_game_still_has_received_and_final_events(self):
        with self.capture() as captured:
            result = self.client.get("/api/games/missing/question")
        self.assertEqual(result.status_code, 404)
        events = self.events(captured)
        self.assertEqual(events[0]["event"], "question_request_received")
        self.assertEqual(events[-1]["status_code"], 404)
        self.assertFalse(self.selected(events, "provider_request_started"))

    def test_openai_and_nvidia_disable_sdk_retries_and_set_timeouts(self):
        for provider, kwargs in [
            ("openai", {"api_key": SECRET, "timeout": 12, "max_retries": 0}),
            ("nvidia", {"api_key": SECRET, "base_url": "https://integrate.api.nvidia.com/v1",
                        "max_retries": 0, "timeout": 12}),
        ]:
            with self.subTest(provider=provider), self.capture():
                LLMClient(provider)
            self.openai.assert_called_with(**kwargs)
        self.sdk.responses.create.return_value = SimpleNamespace(
            status="completed", incomplete_details=None,
            output=[SimpleNamespace(type="message", status="completed", content=[
                SimpleNamespace(type="output_text", text=json.dumps(QUESTION))])])
        self.factory.side_effect = lambda: LLMClient("openai")
        with self.capture() as captured:
            result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.selected(self.events(captured), "provider_request_completed")[0]["provider"], "openai")
        self.assert_safe(captured)

    def test_bedrock_settings_and_provider_events(self):
        sdk = self.boto.return_value
        sdk.meta.config = SimpleNamespace(connect_timeout=3, read_timeout=12,
                                         retries={"mode": "standard", "total_max_attempts": 1})
        sdk.converse.return_value = {"stopReason": "end_turn", "output": {
            "message": {"content": [{"text": json.dumps(QUESTION)}]}}}
        self.factory.side_effect = lambda: LLMClient("bedrock")
        with self.capture() as captured:
            result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200)
        self.boto.assert_called_once()
        args, kwargs = self.boto.call_args
        self.assertEqual(args, ("bedrock-runtime",))
        self.assertEqual(kwargs["region_name"], "us-east-1")
        self.assertEqual(kwargs["config"].retries["total_max_attempts"], 1)
        self.assertEqual(kwargs["config"].read_timeout, 12)
        events = self.events(captured)
        config = self.selected(events, "provider_configuration")[0]
        self.assertEqual(config["sdk_total_max_attempts"], 1)
        self.assertEqual(config["read_timeout_seconds"], 12)
        self.assertEqual(self.selected(events, "provider_request_completed")[0]["provider"], "bedrock")
        self.assert_safe(captured)


if __name__ == "__main__":
    unittest.main()
