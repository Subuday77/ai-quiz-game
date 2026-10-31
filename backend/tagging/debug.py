"""Opt-in bounded debug traces for all generated candidates before storage.

Only generated candidate content is logged; never include user histories,
provider prompts, credentials, request headers or private game state.
"""
from collections.abc import Mapping

from diagnostics import debug_event, generation_debug_enabled

_MAX_STEM = 2500
_MAX_CHOICE = 500
_MAX_SLUG = 140


def _string(value: object, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:max_length]


def _metadata(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    result = {}
    for name in ("entity", "concept", "keyword"):
        items = value.get(name)
        if isinstance(items, list):
            result[name] = [_string(item, _MAX_SLUG) for item in items[:4]
                            if isinstance(item, str)]
    return result


def _fact(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    return {name: _string(value[name], _MAX_SLUG)
            for name in ("subject", "relation", "object")
            if name in value and isinstance(value[name], str)}


def debug_accepted_candidate(candidate: object) -> None:
    """Trace a question that passed generation and quality checks.

    'Accepted' here means accepted by the generator pipeline; persistence
    may still fail, and is deliberately not claimed by this debug event.
    """
    _debug_candidate(candidate, outcome="accepted")


def debug_rejected_candidate(candidate: object, *, reason: str,
                             error_type: str, duplicate_reason: str | None = None) -> None:
    """Trace one rejected structured candidate, if enabled."""
    _debug_candidate(candidate, outcome="rejected", reason=reason,
                     error_type=error_type, duplicate_reason=duplicate_reason)


def _debug_candidate(candidate: object, *, outcome: str,
                     reason: str | None = None, error_type: str | None = None,
                     duplicate_reason: str | None = None) -> None:
    """Shared allowlisted, bounded payload for both outcomes."""
    if not generation_debug_enabled():
        return

    if hasattr(candidate, "model_dump"):
        data = candidate.model_dump()
        report = getattr(candidate, "_review_report", None)
        python_score = getattr(candidate, "_duplicate_assessment", None)
        fact_key = getattr(getattr(candidate, "fact", None), "key", None)
    elif isinstance(candidate, Mapping):
        data = candidate
        report = None
        python_score = None
        fact_key = None
    else:
        return

    answers = data.get("answers")
    safe_answers = ({key: _string(answers[key], _MAX_CHOICE)
                     for key in ("A", "B", "C", "D")
                     if key in answers and isinstance(answers[key], str)}
                    if isinstance(answers, Mapping) else None)

    details = report.get("details", {}) if isinstance(report, dict) else {}
    debug_event(
        "question_candidate_" + outcome,
        outcome=outcome,
        rejection_reason=reason,
        error_type=error_type,
        duplicate_reason=duplicate_reason,
        difficulty=data.get("difficulty") if isinstance(data.get("difficulty"), int) else None,
        question=_string(data.get("question"), _MAX_STEM),
        answers=safe_answers,
        correct_answer=_string(data.get("correct_answer"), 20),
        tags=_metadata(data.get("tags")),
        fact=_fact(data.get("fact")),
        fact_key=fact_key,
        python_duplicate_check=python_score,
        critic={
            "decision": report.get("decision"),
            "verdict": details.get("verdict"),
            "assessment": details.get("assessment"),
            "duration_ms": report.get("duration_ms"),
        } if isinstance(report, dict) else None,
    )
