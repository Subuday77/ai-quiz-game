"""Small, privacy-conscious JSON event logger for question appeals.

QUIZ_COMPLAINT_LOG_LEVEL=INFO (default) or DEBUG. No prompt text, answer
keys, raw model output, names, URLs, API secrets or user comments are logged.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger("quiz.appeals")
LOGGER.propagate = False
_ALLOWED = {
    "appeal_id", "question_id", "stage", "model", "attempt", "elapsed_ms",
    "input_tokens", "output_tokens", "reasoning_tokens", "response_status",
    "stop_reason", "error_type", "error_code", "validation_fields", "decision",
    "status", "next_retry_at", "deposit_tier", "deposit_amount",
    "filing_fee_bronze", "refund_deposit", "compensation_tier",
    "compensation_amount", "expenses_refunded", "wager_refunded",
    "already_applied", "success", "processed",
}


def configure() -> None:
    value = os.getenv("QUIZ_COMPLAINT_LOG_LEVEL", "INFO").strip().upper()
    if value not in ("INFO", "DEBUG"):
        raise ValueError("QUIZ_COMPLAINT_LOG_LEVEL must be INFO or DEBUG")
    LOGGER.setLevel(getattr(logging, value))
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)


def emit(level: str, event: str, **fields: Any) -> None:
    configure()
    numeric = logging.DEBUG if level.upper() == "DEBUG" else logging.INFO
    if level.upper() == "WARNING":
        numeric = logging.WARNING
    elif level.upper() == "ERROR":
        numeric = logging.ERROR
    if not LOGGER.isEnabledFor(numeric):
        return
    safe: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "level": logging.getLevelName(numeric),
    }
    for key, value in fields.items():
        if key not in _ALLOWED or value is None:
            continue
        if isinstance(value, (bool, int, float)):
            safe[key] = value
        elif isinstance(value, (str,)):
            safe[key] = value[:150]
        elif key == "validation_fields" and isinstance(value, (list, tuple)):
            # Only schema field paths and error codes; no personal input values.
            safe[key] = [str(item)[:70] for item in value[:12]]
    LOGGER.log(numeric, json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


def validation_summary(exc: Exception) -> tuple[str, list[str]]:
    """Sanitized reason and paths for debug; never interpolate raw input."""
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_input=False, include_context=False)
        fields = [".".join(map(str, e.get("loc", []))) + ":" +
                  str(e.get("type", "invalid")) for e in errors[:12]]
        return "pydantic", fields
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, ValidationError):
        return validation_summary(cause)
    # Our own validation exceptions are fixed messages, not raw model text.
    reason = str(exc)
    allowed = {
        "Duplicate JSON field", "Non-finite numeric literal in JSON",
        "Response too large or invalid", "Response must be strict JSON",
        "Response must be a JSON object",
        "Payload does not meet the approved schema",
        "Incorrect private answer key", "Invalid verified findings",
    }
    return (reason if reason in allowed else type(exc).__name__), []
