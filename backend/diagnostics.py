"""Content-free JSON diagnostics shared across async requests and worker threads."""

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter

request_id = ContextVar("diagnostic_request_id", default=None)
generation_attempt = ContextVar("generation_attempt", default=None)
log = logging.getLogger("quiz.diagnostics")
# Uvicorn configures its own loggers, not the application's INFO logger.
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False

# Explicit opt-in content-bearing trace. Do NOT enable on shared production
# logs: rejected questions contain answers, and reviewer rationales may quote
# question wording. This logger is deliberately separate from INFO diagnostics.
debug_log = logging.getLogger("quiz.diagnostics.debug")
if not debug_log.handlers:
    debug_handler = logging.StreamHandler()
    debug_handler.setFormatter(logging.Formatter("%(message)s"))
    debug_log.addHandler(debug_handler)
debug_log.setLevel(logging.DEBUG)
debug_log.propagate = False


def generation_debug_enabled() -> bool:
    return os.getenv("AI_GENERATION_DEBUG", "false").strip().lower() == "true"


def debug_event(name: str, **fields) -> None:
    """Opt-in, full-content JSON trace, always prefixed with literal DEBUG:."""
    if not generation_debug_enabled():
        return
    debug_log.debug("DEBUG: %s", json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": name,
        "request_id": request_id.get(),
        "attempt": generation_attempt.get(),
        **fields,
    }, ensure_ascii=False, default=str))



def event(name: str, **fields) -> None:
    """Only pass allowlisted metadata, never bodies, headers or exception text."""
    log.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": name,
        "request_id": request_id.get(),
        "attempt": generation_attempt.get(),
        **fields,
    }))


def elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000, 3)


@contextmanager
def timed_stage(stage: str, **fields):
    """Measure one stage; re-raise errors without logging their contents."""
    start = perf_counter()
    event(f"{stage}_started", **fields)
    try:
        yield
    except Exception as exc:
        event(f"{stage}_failed", duration_ms=elapsed_ms(start),
              error_type=type(exc).__name__, **fields)
        raise
    else:
        event(f"{stage}_completed", duration_ms=elapsed_ms(start), **fields)
