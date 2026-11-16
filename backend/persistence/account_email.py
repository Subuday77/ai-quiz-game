"""Email-account primitives for the staged email-first auth migration.

Not wired into public routes until the schema and email delivery are ready.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone

# Deliberately conservative mailbox syntax; no provider-specific Gmail rules.
_EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+$", re.UNICODE)
_TOKEN_BYTES = 32


def canonical_email(raw: str) -> str:
    """Return the canonical unique-login key. Reject malformed email safely.

    Product policy: case-insensitive mailbox identities; IDNA domains.
    Do not remove plus tags or dots, which could conflate different accounts.
    """
    if not isinstance(raw, str):
        raise ValueError("Invalid email")
    value = unicodedata.normalize("NFKC", raw.strip())
    if len(value) > 254 or not _EMAIL.fullmatch(value) or value.count("@") != 1:
        raise ValueError("Invalid email")
    local, domain = value.rsplit("@", 1)
    if not local or len(local) > 64 or not domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("Invalid email")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid email domain") from exc
    if (len(ascii_domain) > 253 or "." not in ascii_domain
            or any(not part or len(part) > 63 or part.startswith("-")
                   or part.endswith("-") or not re.fullmatch(r"[A-Za-z0-9-]+", part)
                   for part in ascii_domain.split("."))):
        raise ValueError("Invalid email domain")
    return f"{local.casefold()}@{ascii_domain}"


def create_one_time_token() -> tuple[str, str]:
    """Return (secret to email exactly once, SHA-256 digest to persist)."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    return token, token_digest(token)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_expiry(*, minutes: int = 30) -> datetime:
    if minutes < 1 or minutes > 24 * 60:
        raise ValueError("Invalid expiration")
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)
