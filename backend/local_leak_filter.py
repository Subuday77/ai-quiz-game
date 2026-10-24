"""Independent, high-confidence *literal* answer leak gate.

This deliberately does not attempt semantic similarity, stemming or fuzzy
matching. Wrongly rejecting a valid game question is a product defect too.
The legacy critic's richer lexical_findings remains diagnostic/audit-only.
"""
import os
import re
import unicodedata
from typing import Literal

from diagnostics import event
from quality_errors import QualityRegenerationError
from question_schema import QuizQuestion


LeakMode = Literal["off", "audit", "enforce"]


def local_leak_mode_from_env() -> LeakMode:
    mode = os.getenv("AI_LOCAL_LEAK_MODE", "enforce")
    if mode not in ("off", "audit", "enforce"):
        raise ValueError("Invalid AI_LOCAL_LEAK_MODE")
    return mode


def _tokens(text: str) -> tuple[str, ...]:
    # Apply Unicode casefold, remove diacritics/invisible separators,
    # then tokenize so punctuation and whitespace cannot hide literal reuse.
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(c for c in normalized
                         if unicodedata.category(c) not in {"Mn", "Me", "Cf"})
    return tuple(re.findall(r"[^\W_]+", normalized))


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return any(haystack[i:i + len(needle)] == needle
               for i in range(len(haystack) - len(needle) + 1))


def exact_answer_leak(question: QuizQuestion) -> bool:
    """True only for a distinctive verbatim answer in the question stem.

    A quoted *slogan* unrelated to the correct choice remains legitimate.
    Do not block broad semantic or cultural connections (e.g. X-Files).
    This filter is intentionally not a full answer-validity checker.
    """
    stem = _tokens(question.question)
    keyed = _tokens(question.answers[question.correct_answer])
    if not keyed:
        return False
    # A one-letter word is not reliable evidence of answer leakage.
    if len(keyed) == 1 and len(keyed[0]) < 4:
        return False
    if len(keyed) > 1 and sum(map(len, keyed)) < 5:
        return False
    if not _contains(stem, keyed):
        return False
    # If the same phrase appears in two answers, it cannot uniquely
    # give away the keyed choice. Ambiguity is for factual review.
    for choice, answer in question.answers.items():
        if choice != question.correct_answer and _contains(_tokens(answer), keyed):
            return False
    return True


def check_local_leak(question: QuizQuestion, mode: LeakMode) -> None:
    """Run before optional LLM review, including AI_QUALITY_MODE=off/audit.

    Only safe issue codes go to diagnostics: no stem, options or answer key.
    The existing single bounded API retry loop handles regeneration.
    """
    if mode == "off":
        return
    leaked = exact_answer_leak(question)
    event("local_leak_check", mode=mode, finding="answer_leak" if leaked else "none")
    if leaked and mode == "enforce":
        event("quality_question_rejected", mode=mode, decision="regenerate",
              issue_codes=["answer_leak"], source="local_precheck")
        raise QualityRegenerationError()
