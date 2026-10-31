"""Validated server-to-server client for the existing standalone AI Localizer.

Never expose the Localizer URL or credentials to Angular.
"""
import os
from urllib.parse import urlsplit

import httpx


class LocalizationUnavailable(RuntimeError):
    pass


def _endpoint() -> str | None:
    base = os.getenv("AI_LOCALIZER_BASE_URL", "").strip().rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise LocalizationUnavailable("Invalid AI_LOCALIZER_BASE_URL")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise LocalizationUnavailable("The remote Localizer must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalizationUnavailable("Localizer URL must not contain credentials or query")
    return base + "/translate"


async def translate_missing(missing: dict[str, str], code: str) -> dict[str, str]:
    endpoint = _endpoint()
    if not endpoint:
        raise LocalizationUnavailable("AI_LOCALIZER_BASE_URL is not configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=8.0),
                                     follow_redirects=False) as client:
            response = await client.post(
                endpoint, json={"dictionary": missing, "language_code": code},
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
    return translated
