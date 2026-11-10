"""Server-side configurable order for choosing a new question.

No client parameter is trusted to choose generation or reuse. Strict tagged
approval, exact category/difficulty/language and user-level dedup remain
mandatory in either order.
"""
import os


def question_source_order() -> str:
    value = os.getenv("QUIZ_QUESTION_SOURCE_ORDER", "generate_first").strip().lower()
    if value not in {"generate_first", "pool_first"}:
        raise ValueError(
            "QUIZ_QUESTION_SOURCE_ORDER must be generate_first or pool_first"
        )
    return value


def community_vote_threshold() -> int:
    raw = os.getenv("QUIZ_COMMUNITY_VOTE_THRESHOLD", "-12").strip()
    try:
        threshold = int(raw)
    except ValueError:
        raise ValueError("QUIZ_COMMUNITY_VOTE_THRESHOLD must be an integer") from None
    if not -100000 <= threshold <= -1:
        raise ValueError("QUIZ_COMMUNITY_VOTE_THRESHOLD must be in -100000..-1")
    return threshold
