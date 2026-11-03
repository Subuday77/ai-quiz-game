"""Independent knobs for shadow correctness risk and tagged editorial scoring.

Read settings at call-time (not import-time) so deployments and offline tests can
change one parameter without changing another. All settings are server-side.
"""
import math
import os
from dataclasses import dataclass


def _number(name: str, default: float, *, low: float = 0.0,
            high: float = 100.0, strictly_positive: bool = False) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        result = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(result) or result < low or result > high or (
        strictly_positive and result == 0
    ):
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


@dataclass(frozen=True)
class SkillSettings:
    mode: str
    lexical_penalty_weight: float
    taxonomy_penalty_weight: float
    fact_penalty_weight: float
    arbitration_min_severity: int
    arbitration_min_confidence: float
    lexical_min_prefix: int
    lexical_min_shared_fraction: float

    @classmethod
    def from_env(cls):
        mode = os.getenv("QUIZ_QUALITY_SKILL_MODE", "shadow").strip().lower()
        if mode not in {"off", "shadow"}:
            raise ValueError("QUIZ_QUALITY_SKILL_MODE must be off|shadow")
        severity = _number("QUIZ_QUALITY_ARBITRATION_MIN_SEVERITY", 2, low=1, high=3)
        if not severity.is_integer():
            raise ValueError("QUIZ_QUALITY_ARBITRATION_MIN_SEVERITY must be an integer")
        prefix = _number("QUIZ_QUALITY_LEXICAL_MIN_PREFIX", 5, low=3, high=12)
        if not prefix.is_integer():
            raise ValueError("QUIZ_QUALITY_LEXICAL_MIN_PREFIX must be an integer")
        return cls(
            mode=mode,
            lexical_penalty_weight=_number("QUIZ_QUALITY_LEXICAL_PENALTY_WEIGHT", 3.0),
            taxonomy_penalty_weight=_number("QUIZ_QUALITY_TAXONOMY_PENALTY_WEIGHT", 3.0),
            fact_penalty_weight=_number("QUIZ_QUALITY_FACT_PENALTY_WEIGHT", 3.0),
            arbitration_min_severity=int(severity),
            arbitration_min_confidence=_number(
                "QUIZ_QUALITY_ARBITRATION_MIN_CONFIDENCE", 0.55, high=1.0),
            lexical_min_prefix=int(prefix),
            lexical_min_shared_fraction=_number(
                "QUIZ_QUALITY_LEXICAL_MIN_SHARED_FRACTION", 0.8, high=1.0),
        )


@dataclass(frozen=True)
class EditorialSettings:
    score_threshold: float
    distractors_weight: float
    early_distractors_weight: float
    suitability_weight: float
    wording_weight: float
    ordinary_knowledge_unit: float
    ordinary_knowledge_cap: float
    too_hard_unit: float
    too_hard_cap: float
    too_easy_unit_start: float
    too_easy_unit_end: float
    too_easy_cap_start: float
    too_easy_cap_end: float
    too_easy_curve: float

    @classmethod
    def from_env(cls):
        return cls(
            score_threshold=_number("QUIZ_QUALITY_SCORE_THRESHOLD", 3.25,
                                    low=0.01, strictly_positive=True),
            distractors_weight=_number("QUIZ_QUALITY_DISTRACTORS_WEIGHT", 1.0),
            early_distractors_weight=_number("QUIZ_QUALITY_EARLY_DISTRACTORS_WEIGHT", 0.5),
            suitability_weight=_number("QUIZ_QUALITY_SUITABILITY_WEIGHT", 1.0),
            wording_weight=_number("QUIZ_QUALITY_WORDING_WEIGHT", 1.0),
            ordinary_knowledge_unit=_number("QUIZ_QUALITY_KNOWLEDGE_UNIT", 0.25),
            ordinary_knowledge_cap=_number("QUIZ_QUALITY_KNOWLEDGE_CAP", 0.5),
            too_hard_unit=_number("QUIZ_QUALITY_TOO_HARD_UNIT", 0.25),
            too_hard_cap=_number("QUIZ_QUALITY_TOO_HARD_CAP", 0.5),
            too_easy_unit_start=_number("QUIZ_QUALITY_TOO_EASY_UNIT_START", 0.25),
            too_easy_unit_end=_number("QUIZ_QUALITY_TOO_EASY_UNIT_END", 1.5),
            too_easy_cap_start=_number("QUIZ_QUALITY_TOO_EASY_CAP_START", 0.5),
            too_easy_cap_end=_number("QUIZ_QUALITY_TOO_EASY_CAP_END", 3.0),
            too_easy_curve=_number("QUIZ_QUALITY_TOO_EASY_CURVE", 1.5,
                                   low=0.01, high=8.0, strictly_positive=True),
        )
