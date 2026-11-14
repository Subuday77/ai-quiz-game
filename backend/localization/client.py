"""Validated server-to-server client for the existing standalone AI Localizer.

Never expose the Localizer URL or credentials to Angular. The VPS connection
uses the dedicated internal Docker network, not public Caddy endpoints.
"""
import os
import re
from urllib.parse import urlsplit

import httpx


class LocalizationUnavailable(RuntimeError):
    pass


_INTERNAL_LOCALIZER_HOST = "localizer-api"
_INTERNAL_LOCALIZER_PORT = 8080


def _endpoint() -> str | None:
    base = os.getenv("AI_LOCALIZER_BASE_URL", "").strip().rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise LocalizationUnavailable("Invalid AI_LOCALIZER_BASE_URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise LocalizationUnavailable("Invalid AI_LOCALIZER_BASE_URL port") from exc
    internal_http = (
        parsed.scheme == "http"
        and parsed.hostname == _INTERNAL_LOCALIZER_HOST
        and port == _INTERNAL_LOCALIZER_PORT
        and not parsed.path
    )
    if (parsed.scheme != "https"
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            and not internal_http):
        raise LocalizationUnavailable("The remote Localizer must use HTTPS or the dedicated Docker network")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalizationUnavailable("Localizer URL must not contain credentials or query")
    return base + "/translate"


async def translate_missing(missing: dict[str, str], code: str) -> dict[str, str]:
    endpoint = _endpoint()
    if not endpoint:
        raise LocalizationUnavailable("AI_LOCALIZER_BASE_URL is not configured")

    # Only server-side environment variables may supply a Localizer credential.
    # Never store one in frontend assets, URLs, request logs, or exceptions.
    api_key = os.getenv("AI_LOCALIZER_API_KEY", "").strip()
    internal_http = endpoint == f"http://{_INTERNAL_LOCALIZER_HOST}:{_INTERNAL_LOCALIZER_PORT}/translate"
    if internal_http and not api_key:
        raise LocalizationUnavailable("AI_LOCALIZER_API_KEY is required for internal Localizer")
    # Never forward the internal key to a legacy/rollback HTTPS endpoint.
    headers = {"Authorization": f"Bearer {api_key}"} if internal_http else {}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=8.0),
            follow_redirects=False,
            trust_env=not internal_http,  # Never send private tokens through HTTP_PROXY.
        ) as client:
            response = await client.post(
                endpoint,
                json={"dictionary": missing, "language_code": code},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LocalizationUnavailable("AI Localizer request failed") from exc
    if not isinstance(payload, dict) or payload.get("fallback") is not False:
        raise LocalizationUnavailable("AI Localizer returned English fallback")
    translated = payload.get("dictionary")
    if (not isinstance(translated, dict) or set(translated) != set(missing)
            or any(not isinstance(s, str) or not s.strip() or len(s) > 4000
                   for s in translated.values())):
        raise LocalizationUnavailable("Invalid AI Localizer response")
    placeholder = re.compile(r"\{\{[a-zA-Z0-9_]+\}\}")
    for key, original in missing.items():
        if sorted(placeholder.findall(original)) != sorted(placeholder.findall(translated[key])):
            raise LocalizationUnavailable("AI Localizer altered a UI placeholder")
    return translated
