"""Offline tests for the VPS Localizer server-to-server connection."""

import os
import unittest
from unittest.mock import patch

import httpx

from localization.client import LocalizationUnavailable, _endpoint, translate_missing


class LocalizerPrivateClientTests(unittest.IsolatedAsyncioTestCase):
    def test_allows_only_exact_private_docker_hostname_over_http(self):
        with patch.dict(os.environ, {"AI_LOCALIZER_BASE_URL": "http://localizer-api:8080"}):
            self.assertEqual(_endpoint(), "http://localizer-api:8080/translate")

        for url in (
            "http://localizer-api:8090",
            "http://localizer-api",
            "http://localizer-api:8080/proxy",
            "http://example.com",
            "http://10.0.0.2:8080",
            "http://localizer-api:broken",
            "http://localizer-api:8080@evil.example",
        ):
            with self.subTest(url=url), patch.dict(os.environ, {"AI_LOCALIZER_BASE_URL": url}):
                with self.assertRaises(LocalizationUnavailable):
                    _endpoint()

    def test_legacy_render_https_is_still_allowed(self):
        with patch.dict(os.environ, {"AI_LOCALIZER_BASE_URL": "https://ai-localizer-api.onrender.com"}):
            self.assertEqual(_endpoint(), "https://ai-localizer-api.onrender.com/translate")

    async def test_private_http_refuses_to_send_without_key(self):
        with patch.dict(os.environ, {"AI_LOCALIZER_BASE_URL": "http://localizer-api:8080", "AI_LOCALIZER_API_KEY": ""}):
            with self.assertRaisesRegex(LocalizationUnavailable, "AI_LOCALIZER_API_KEY"):
                await translate_missing({"hello": "Hello"}, "en")

    async def test_internal_client_sends_secret_only_as_authorization_header(self):
        token = "private-integration-test-token"
        seen = []

        async def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={
                "dictionary": {"greet": "Hallo {{name}}"},
                "fallback": False,
            })

        original_client = httpx.AsyncClient
        mock_client = original_client(transport=httpx.MockTransport(handle))

        with patch.dict(os.environ, {
            "AI_LOCALIZER_BASE_URL": "http://localizer-api:8080",
            "AI_LOCALIZER_API_KEY": token,
        }), patch("localization.client.httpx.AsyncClient", return_value=mock_client) as factory:
            result = await translate_missing({"greet": "Hello {{name}}"}, "de")

        self.assertEqual(result, {"greet": "Hallo {{name}}"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), "http://localizer-api:8080/translate")
        self.assertEqual(seen[0].headers["authorization"], f"Bearer {token}")
        self.assertNotIn(token, str(seen[0].url))
        self.assertFalse(factory.call_args.kwargs["trust_env"])

    async def test_render_rollback_does_not_receive_private_api_key(self):
        seen = []

        async def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={
                "dictionary": {"greet": "Hallo"},
                "fallback": False,
            })

        original_client = httpx.AsyncClient
        mock_client = original_client(transport=httpx.MockTransport(handle))
        with patch.dict(os.environ, {
            "AI_LOCALIZER_BASE_URL": "https://ai-localizer-api.onrender.com",
            "AI_LOCALIZER_API_KEY": "private-do-not-send-to-render",
        }), patch("localization.client.httpx.AsyncClient", return_value=mock_client):
            result = await translate_missing({"greet": "Hello"}, "de")

        self.assertEqual(result, {"greet": "Hallo"})
        self.assertNotIn("authorization", seen[0].headers)

    async def test_invalid_response_placeholders_never_enter_cache(self):
        async def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "dictionary": {"greet": "Hallo {{other}}"},
                "fallback": False,
            })

        original_client = httpx.AsyncClient
        mock_client = original_client(transport=httpx.MockTransport(handle))
        with patch.dict(os.environ, {
            "AI_LOCALIZER_BASE_URL": "http://localizer-api:8080",
            "AI_LOCALIZER_API_KEY": "fixture-key",
        }), patch("localization.client.httpx.AsyncClient", return_value=mock_client):
            with self.assertRaisesRegex(LocalizationUnavailable, "placeholder"):
                await translate_missing({"greet": "Hello {{name}}"}, "de")


if __name__ == "__main__":
    unittest.main()
