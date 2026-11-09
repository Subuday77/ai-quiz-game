"""Pure appeal-policy decisions. No AI calls or side effects here.

The caller must serialize wallet mutations and question state transitions in a
single database transaction. Fees and stake are distinct ledger operations.
"""
from __future__ import annotations

from datetime import datetime, timezone

FREE_MONTHLY_APPEALS = 5
RETRY_DAYS = 7
MAX_IMMEDIATE_JUDGE_ATTEMPTS = 3

REASONS_BEFORE_ANSWER = frozenset({
    "multiple_correct", "no_correct", "ambiguous", "bad_translation",
    "answer_leak", "difficulty_mismatch", "other",
})
REASONS_AFTER_ANSWER = REASONS_BEFORE_ANSWER | {"incorrect_key"}


def month_bounds(when: datetime) -> tuple[datetime, datetime]:
    when = when.astimezone(timezone.utc)
    start = datetime(when.year, when.month, 1, tzinfo=timezone.utc)
    year, month = when.year + (when.month == 12), (when.month % 12) + 1
    return start, datetime(year, month, 1, tzinfo=timezone.utc)


def appeal_fee_bronze(already_submitted_this_month: int) -> int:
    """N previous filings: #1-5 free, #6 costs 1, #7 costs 2, etc."""
    if already_submitted_this_month < 0:
        raise ValueError("Invalid appeal count")
    if already_submitted_this_month < FREE_MONTHLY_APPEALS:
        return 0
    return 1 << (already_submitted_this_month - FREE_MONTHLY_APPEALS)


def stake_tier(question_number: int) -> str:
    if not 1 <= question_number <= 16:
        raise ValueError("Invalid question number")
    return "bronze" if question_number <= 5 else "silver" if question_number <= 10 else "gold"


def validate_reasons(reasons: list[str], *, before_answer: bool) -> tuple[str, ...]:
    allowed = REASONS_BEFORE_ANSWER if before_answer else REASONS_AFTER_ANSWER
    if not reasons or len(reasons) != len(set(reasons)) or any(r not in allowed for r in reasons):
        raise ValueError("Invalid complaint reasons for answer stage")
    return tuple(reasons)


def automatic_uphold(previous_upheld: int) -> bool:
    return previous_upheld >= 2


def permanent_block(*, distinct_players: int, upheld: int) -> str | None:
    if upheld > 0:
        return "confirmed_error"
    if distinct_players >= 3:
        return "complaint_threshold"
    return None


def compensation_allowed(*, lost_question_id: str | None, appealed_question_id: str) -> bool:
    """Only a loss on the very same immutable question grants a reward."""
    return lost_question_id is not None and lost_question_id == appealed_question_id


def stake_refund_required(*, upheld: bool, technical_unresolved: bool = False) -> bool:
    """The stake is separate from a never-refundable filing fee."""
    return upheld or technical_unresolved
