"""Single validated configuration boundary for Platinum Helper.

The repository-root .env.example documents every overridable setting.
Read at call time so tests and server processes use identical parameters.
Defaults are centralized here for backwards compatibility with older .env
files; explicit invalid values always fail instead of being silently ignored.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _integer(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer between {low} and {high}") from None
    if value < low or value > high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _float(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be between {low} and {high}") from None
    if not math.isfinite(value) or value < low or value > high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class HelperSettings:
    model: str
    reasoning_effort: str
    max_output_tokens: int
    timeout_seconds: float
    threshold_level_0: int
    threshold_levels_1_3: int
    threshold_levels_4_15: int

    @property
    def max_attempts(self) -> int:
        """Read-only API budget for older offline QA runners: always ONE call.

        Unlike the removed QUIZ_HELPER_MAX_ATTEMPTS env variable, this
        compatibility property cannot enable retries.
        """
        return 1

    @classmethod
    def from_env(cls) -> "HelperSettings":
        model = os.getenv("QUIZ_HELPER_MODEL_ID", "gpt-5.4").strip()
        if not model or len(model) > 130 or any(ch.isspace() for ch in model):
            raise ValueError("QUIZ_HELPER_MODEL_ID must be a nonempty model identifier")
        effort = os.getenv("QUIZ_HELPER_REASONING_EFFORT", "low").strip().lower()
        if effort not in {"none", "low", "medium", "high"}:
            raise ValueError("QUIZ_HELPER_REASONING_EFFORT must be none|low|medium|high")
        thresholds = (
            _integer("QUIZ_HELPER_PLAUSIBILITY_LEVEL_0", 40, 0, 100),
            _integer("QUIZ_HELPER_PLAUSIBILITY_LEVELS_1_3", 50, 0, 100),
            _integer("QUIZ_HELPER_PLAUSIBILITY_LEVELS_4_15", 60, 0, 100),
        )
        if sorted(thresholds) != list(thresholds):
            raise ValueError("Helper plausibility thresholds must be nondecreasing")
        return cls(
            model=model,
            reasoning_effort=effort,
            max_output_tokens=_integer("QUIZ_HELPER_MAX_OUTPUT_TOKENS", 240, 64, 4096),
            timeout_seconds=_float("QUIZ_HELPER_TIMEOUT_SECONDS", 8.0, 0.1, 120.0),
            threshold_level_0=thresholds[0],
            threshold_levels_1_3=thresholds[1],
            threshold_levels_4_15=thresholds[2],
        )
