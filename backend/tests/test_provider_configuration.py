"""Real SDK construction regressions; dummy credentials and no network requests."""

import json
import unittest
from unittest.mock import patch

from openai import OpenAI, Timeout

from define_ai_provider import LLMClient
from diagnostics import generation_attempt, request_id
from generation_policy import provider_timeout


DUMMY_KEY = "offline-dummy-key-do-not-log"
SOCKET_NAMES = ("connect", "read", "write", "pool")


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {
            "OPENAI_API_KEY": DUMMY_KEY, "OPENAI_MODEL_ID": "offline-model",
            "NVIDIA_API_KEY": DUMMY_KEY, "NVIDIA_MODEL_ID": "offline-model",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12",
            "AI_GENERATION_MAX_ATTEMPTS": "3", "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5", "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        }))
        # Keep the actual constructor and timeout representation. Block both SDK
        # requests and sockets so this regression cannot accidentally call an API.
        self.requests = self.enterContext(patch.object(
            OpenAI, "request", side_effect=AssertionError("Unexpected SDK request")))
        self.connections = self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("Unexpected network access")))
        self.bedrock = self.enterContext(patch(
            "define_ai_provider.boto3.client", side_effect=AssertionError("Unexpected AWS client")))
        for variable, value in ((request_id, "offline-correlation"),
                                (generation_attempt, 2), (provider_timeout, None)):
            token = variable.set(value)
            self.addCleanup(variable.reset, token)

    def construct_and_check(self, provider, expected):
        with self.assertLogs("quiz.diagnostics", level="INFO") as captured:
            adapter = LLMClient(provider)
        self.addCleanup(adapter.client.close)
        self.assertIsInstance(adapter.client, OpenAI)
        self.assertEqual(adapter.client.max_retries, 0)
        self.assertEqual(adapter.model, "offline-model")
        self.assertIs(adapter.responses, adapter)
        events = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "provider_configuration")
        self.assertEqual(events[0]["provider"], provider)
        self.assertEqual(events[0]["timeout_seconds"], expected)
        self.assertEqual(events[0]["sdk_max_retries"], 0)
        self.assertEqual(events[0]["request_id"], "offline-correlation")
        self.assertEqual(events[0]["attempt"], 2)
        self.assertNotIn(DUMMY_KEY, "\n".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))
        self.requests.assert_not_called()
        self.connections.assert_not_called()
        self.bedrock.assert_not_called()
        return adapter

    def test_openai_real_client_numeric_timeout(self):
        adapter = self.construct_and_check("openai", dict.fromkeys(SOCKET_NAMES, 12.0))
        self.assertEqual(adapter.client.timeout, 12.0)

    def test_nvidia_real_client_numeric_timeout(self):
        adapter = self.construct_and_check("nvidia", dict.fromkeys(SOCKET_NAMES, 12.0))
        self.assertEqual(adapter.client.timeout, 12.0)
        self.assertEqual(str(adapter.client.base_url), "https://integrate.api.nvidia.com/v1/")

    def test_real_clients_keep_configured_and_remaining_budget_timeouts(self):
        for provider in ("openai", "nvidia"):
            with self.subTest(provider=provider), patch.dict(
                    "os.environ", {"AI_PROVIDER_TIMEOUT_SECONDS": "7.25"}):
                adapter = self.construct_and_check(provider, dict.fromkeys(SOCKET_NAMES, 7.25))
                self.assertEqual(adapter.client.timeout, 7.25)
                token = provider_timeout.set(1.75)
                try:
                    adapter = self.construct_and_check(provider, dict.fromkeys(SOCKET_NAMES, 1.75))
                    self.assertEqual(adapter.client.timeout, 1.75)
                finally:
                    provider_timeout.reset(token)

    def test_real_clients_with_structured_timeout_also_log_all_socket_limits(self):
        # Use the SDK's public Timeout export (httpx/httpx2 according to SDK
        # version), rather than a fake object with preconfigured attributes.
        structured = Timeout(connect=2.0, read=7.0, write=4.0, pool=1.0)
        expected = {name: getattr(structured, name) for name in SOCKET_NAMES}

        def real_client(**kwargs):
            kwargs["timeout"] = structured
            return OpenAI(**kwargs)

        with patch("define_ai_provider.OpenAI", side_effect=real_client):
            for provider in ("openai", "nvidia"):
                with self.subTest(provider=provider):
                    adapter = self.construct_and_check(provider, expected)
                    self.assertIs(adapter.client.timeout, structured)


if __name__ == "__main__":
    unittest.main()
