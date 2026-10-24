"""Offline retry, budget, concurrency and browser-abandonment recovery tests."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, ReadTimeoutError
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError

from api import app, sessions
from generation_policy import GenerationPolicy, is_transient, provider_timeout
from test_api import QUESTION


def status_error(status, headers=None):
    return APIStatusError("sensitive provider message", body=None,
                          response=Mock(status_code=status, headers=headers or {}, request=Mock()))


class RetryTests(unittest.TestCase):
    def setUp(self):
        sessions.clear()
        self.client = TestClient(app)
        self.game_id = self.client.post("/api/games", json={}).json()["game_id"]
        self.url = f"/api/games/{self.game_id}/question"
        self.clock = [100.0]
        self.enterContext(patch("api.perf_counter", side_effect=lambda: self.clock[0]))
        self.sleep = self.enterContext(patch("api.sleep", side_effect=self.advance))
        self.enterContext(patch("generation_policy.random.uniform", side_effect=lambda low, high: high))
        self.enterContext(patch.dict("os.environ", {
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_QUALITY_MODE": "off", "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        }))
        self.generated = self.enterContext(patch("api.create_question", return_value=QUESTION))
        self.enterContext(patch("diagnostics.log.info"))

    def advance(self, seconds):
        self.clock[0] += seconds

    def assert_unchanged(self, categories, progress=0):
        session = sessions[self.game_id]
        self.assertEqual(session.categories, categories)
        self.assertEqual(session.state.correct_main_answers, progress)
        self.assertEqual(session.state.current_difficulty, progress)
        self.assertIsNone(session.question)
        self.assertIsNone(session.question_id)

    def test_timeouts_connections_and_transient_statuses_recover(self):
        errors = [APITimeoutError(request=Mock()), APIConnectionError(request=Mock()),
                  ReadTimeoutError(endpoint_url="offline"), status_error(408),
                  status_error(429), status_error(500), status_error(503)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                sessions[self.game_id].question = None
                sessions[self.game_id].question_id = None
                self.generated.reset_mock()
                self.generated.side_effect = [error, QUESTION]
                result = self.client.get(self.url)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.generated.call_count, 2)

    def test_permanent_errors_fail_once_without_sleep_or_state_change(self):
        categories = sessions[self.game_id].categories.copy()
        errors = [status_error(s) for s in (400, 401, 403, 404, 409, 422)]
        errors += [ValueError("unsupported provider/parameter"), KeyError("missing credential"),
                   RuntimeError("unknown failure"),
                   ClientError({"Error": {"Code": "ValidationException"},
                                "ResponseMetadata": {"HTTPStatusCode": 400}}, "Converse")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.generated.reset_mock()
                self.generated.side_effect = error
                result = self.client.get(self.url)
                self.assertEqual(result.status_code, 503)
                self.generated.assert_called_once()
                self.sleep.assert_not_called()
                self.assert_unchanged(categories)

    def test_retry_exhaustion_is_three_total_calls_and_two_bounded_sleeps(self):
        categories = sessions[self.game_id].categories.copy()
        self.generated.side_effect = APITimeoutError(request=Mock())
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.generated.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])
        self.assert_unchanged(categories)

    def test_validation_and_provider_retries_share_one_limit(self):
        categories = sessions[self.game_id].categories.copy()
        self.generated.side_effect = [status_error(503), {**QUESTION, "difficulty": 8}, QUESTION]
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.generated.call_count, 3)
        self.assertEqual(sessions[self.game_id].categories, categories[:-1])
        self.assertEqual(len(set(c.args[2] for c in self.generated.call_args_list)), 1)
        self.assertEqual(result.json()["difficulty"], 0)

    def test_budget_shrinks_timeout_and_stops_additional_attempts(self):
        timeouts = []
        def slow(*args):
            timeouts.append(provider_timeout.get())
            self.advance(20)
            raise APITimeoutError(request=Mock())
        self.generated.side_effect = slow
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.generated.call_count, 2)
        self.assertEqual(timeouts, [12, 9.5])
        self.assertIsNone(provider_timeout.get())

    def test_no_sleep_or_new_call_when_budget_is_exhausted(self):
        def slow(*args):
            self.advance(30)
            raise TimeoutError()
        self.generated.side_effect = slow
        self.assertEqual(self.client.get(self.url).status_code, 503)
        self.generated.assert_called_once()
        self.sleep.assert_not_called()

    def test_configurable_attempt_limit(self):
        self.generated.side_effect = TimeoutError()
        with patch.dict("os.environ", {"AI_GENERATION_MAX_ATTEMPTS": "2"}):
            self.assertEqual(self.client.get(self.url).status_code, 503)
        self.assertEqual(self.generated.call_count, 2)

    def test_retry_after_longer_than_limit_is_not_ignored(self):
        self.generated.side_effect = status_error(429, {"retry-after": "60"})
        self.assertEqual(self.client.get(self.url).status_code, 503)
        self.generated.assert_called_once()
        self.sleep.assert_not_called()

    def test_retry_after_within_limit_is_respected(self):
        self.generated.side_effect = [status_error(429, {"retry-after": "1.5"}), QUESTION]
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.sleep.assert_called_once_with(1.5)

    def test_concurrent_browser_retry_reuses_in_flight_question_and_reward_once(self):
        entered, release, waiting = Event(), Event(), Event()
        real_lock = sessions[self.game_id].lock
        class ObservedLock:
            def __enter__(self):
                if real_lock.locked():
                    waiting.set()
                real_lock.acquire()
            def __exit__(self, *args):
                real_lock.release()
        sessions[self.game_id].lock = ObservedLock()
        # Start just before the bronze threshold to verify reward allocation too.
        sessions[self.game_id].state.correct_main_answers = 4
        question = {**QUESTION, "difficulty": 4}
        categories = sessions[self.game_id].categories.copy()
        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test failed to release provider")
            return question
        self.generated.side_effect = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            abandoned = pool.submit(self.client.get, self.url)
            try:
                self.assertTrue(entered.wait(5))
                # The browser has stopped waiting; server-side work still owns the lock.
                retry = pool.submit(self.client.get, self.url)
                self.assertTrue(waiting.wait(5))
                self.assertFalse(retry.done())
            finally:
                release.set()
            first = abandoned.result(timeout=5)
            recovered = retry.result(timeout=5)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), recovered.json())
        self.generated.assert_called_once()
        self.assertEqual(sessions[self.game_id].categories, categories[:-1])
        self.assertEqual(sessions[self.game_id].state.correct_main_answers, 4)
        payload = {"question_id": recovered.json()["question_id"], "answer": "B"}
        answer_url = f"/api/games/{self.game_id}/answer"
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.client.post(answer_url, json=payload), range(2)))
        self.assertEqual(sorted(r.status_code for r in replies), [200, 409])
        successful = next(r for r in replies if r.status_code == 200)
        self.assertEqual(successful.json()["achievement"], "bronze")
        self.assertEqual(sessions[self.game_id].state.correct_main_answers, 5)

    def test_validation_exhaustion_preserves_category_for_manual_retry(self):
        categories = sessions[self.game_id].categories.copy()
        self.generated.return_value = {**QUESTION, "answers": {"A": "incomplete"}}
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.generated.call_count, 3)
        self.assert_unchanged(categories)
        self.generated.return_value = QUESTION
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(sessions[self.game_id].categories, categories[:-1])
        self.assertEqual(len(set(c.args[2] for c in self.generated.call_args_list)), 1)

    def test_late_success_is_cached_for_recovery_after_browser_timeout(self):
        def slow(*args):
            self.advance(50)
            return QUESTION
        self.generated.side_effect = slow
        original = self.client.get(self.url)
        self.assertEqual(original.status_code, 200)
        recovered = self.client.get(self.url)
        self.assertEqual(recovered.json(), original.json())
        self.generated.assert_called_once()

    def test_invalid_browser_configuration_returns_safe_error(self):
        for warning, timeout in [("45", "45"), ("nan", "45"), ("8", "0"),
                                 ("8", "3000000"), ("8", "1e308")]:
            with self.subTest(warning=warning, timeout=timeout), patch.dict("os.environ", {
                "QUIZ_LONG_WAIT_WARNING_SECONDS": warning, "QUIZ_HTTP_TIMEOUT_SECONDS": timeout,
            }):
                self.assertEqual(self.client.get("/api/client-config").status_code, 503)

    def test_public_config_exposes_only_validated_timing_settings(self):
        with patch.dict("os.environ", {"QUIZ_LONG_WAIT_WARNING_SECONDS": "4",
                                       "QUIZ_HTTP_TIMEOUT_SECONDS": "20"}):
            result = self.client.get("/api/client-config")
        self.assertEqual(result.json(), {"long_wait_warning_ms": 4000, "request_timeout_ms": 20000})

    def test_invalid_policy_fails_without_provider_or_state_mutation(self):
        categories = sessions[self.game_id].categories.copy()
        for value in ("0", "nan", "inf", "wrong", "1.5"):
            with self.subTest(value=value), patch.dict("os.environ", {"AI_GENERATION_MAX_ATTEMPTS": value}):
                self.assertEqual(self.client.get(self.url).status_code, 503)
        self.generated.assert_not_called()
        self.assert_unchanged(categories)


class PolicyTests(unittest.TestCase):
    def test_backoff_exponential_jitter_and_cap(self):
        policy = GenerationPolicy()
        with patch("generation_policy.random.uniform", side_effect=lambda low, high: high) as jitter:
            self.assertEqual([policy.backoff(n) for n in (1, 2, 3, 4, 999)], [0.5, 1, 2, 2, 2])
            self.assertEqual(jitter.call_args_list[0].args, (0.25, 0.5))
            self.assertEqual(jitter.call_args_list[-1].args, (1, 2))

    def test_bedrock_authentication_and_transient_classification(self):
        for code, status, expected in [("ThrottlingException", 429, True),
                                       ("ModelNotReadyException", 429, True),
                                       ("InternalServerException", 500, True),
                                       ("AccessDeniedException", 403, False),
                                       ("ValidationException", 400, False),
                                       ("ResourceNotFoundException", 404, False)]:
            with self.subTest(code=code):
                exc = ClientError({"Error": {"Code": code},
                                   "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")
                self.assertEqual(is_transient(exc), expected)


if __name__ == "__main__":
    unittest.main()
