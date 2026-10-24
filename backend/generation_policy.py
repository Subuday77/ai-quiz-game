"""Interactive generation settings and safe provider failure classification."""

import math
import os
import random
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError,
                                 EndpointConnectionError, ReadTimeoutError)
from openai import APIConnectionError, APIStatusError

from provider_result import EmptyGenerationError, TruncatedGenerationError
from quality_errors import QualityReviewFailure

# Set only while an API generation attempt is running; CLI calls use the default.
provider_timeout = ContextVar("provider_timeout", default=None)


def env_number(name: str, default: float, *, integer=False, minimum=0):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= minimum or (integer and not value.is_integer()):
        raise ValueError("Invalid generation setting")
    return int(value) if integer else value


@dataclass(frozen=True)
class GenerationPolicy:
    provider_timeout_seconds: float = 12
    max_attempts: int = 3
    budget_seconds: float = 30
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 2

    @classmethod
    def from_env(cls):
        return cls(
            env_number("AI_PROVIDER_TIMEOUT_SECONDS", 12),
            env_number("AI_GENERATION_MAX_ATTEMPTS", 3, integer=True),
            env_number("AI_GENERATION_BUDGET_SECONDS", 30),
            env_number("AI_RETRY_BACKOFF_BASE_SECONDS", 0.5),
            env_number("AI_RETRY_BACKOFF_MAX_SECONDS", 2),
        )

    def backoff(self, failed_attempt: int) -> float:
        # Clamp the exponent as well as the result, even with large configured limits.
        ceiling = min(self.backoff_max_seconds,
                      self.backoff_base_seconds * 2 ** min(failed_attempt - 1, 30))
        return random.uniform(ceiling / 2, ceiling)


class InvalidQuestionError(ValueError):
    """A generated question failed an application content check."""


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, QualityReviewFailure):
        return exc.retryable
    if isinstance(exc, (TruncatedGenerationError, EmptyGenerationError,
                        APIConnectionError, ConnectTimeoutError, ReadTimeoutError,
                        EndpointConnectionError, ConnectionClosedError,
                        TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (408, 429) or 500 <= exc.status_code < 600
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code in {"AccessDeniedException", "UnrecognizedClientException",
                    "InvalidSignatureException", "ExpiredTokenException",
                    "ValidationException", "ResourceNotFoundException"}:
            return False
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in {"ThrottlingException", "TooManyRequestsException",
                        "ServiceUnavailableException", "InternalServerException",
                        "ModelTimeoutException", "ModelNotReadyException",
                        "RequestTimeout"} or status in (408, 429) or 500 <= status < 600
    # Unknown exceptions and configuration/parameter errors are not retried.
    return False


def retry_after_seconds(exc: Exception) -> float | None:
    """Read only the retry delay; never log headers or exception messages."""
    if isinstance(exc, QualityReviewFailure):
        return exc.retry_after
    if not isinstance(exc, APIStatusError):
        return None
    value = exc.response.headers.get("retry-after")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


def client_settings() -> dict[str, int]:
    warning = env_number("QUIZ_LONG_WAIT_WARNING_SECONDS", 8)
    timeout = env_number("QUIZ_HTTP_TIMEOUT_SECONDS", 45)
    if not 0 < warning < timeout <= 2_147_483.647:
        raise ValueError("Invalid browser timing settings")
    warning_ms = round(warning * 1000)
    timeout_ms = round(timeout * 1000)
    if not 0 < warning_ms < timeout_ms <= 2_147_483_647:
        raise ValueError("Invalid browser timing settings")
    return {"long_wait_warning_ms": warning_ms, "request_timeout_ms": timeout_ms}
