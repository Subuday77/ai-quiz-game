"""Pure, side-effect-free rules for question appeals.

A bonus-round wager is *not* an appeal deposit. Filing fees are an entirely
separate nonrefundable debit. All wallet operations belong to one DB transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

FREE_MONTHLY_APPEALS = 5
MAX_IMMEDIATE_JUDGE_ATTEMPTS = 3
RETRY_DAYS = 7
MAX_COMMENT_LENGTH = 1000

# Deliberately no "bad_translation" or "difficulty_mismatch" checkbox.
# Players may explain either concern in the free-text comment.
REASONS_BEFORE_ANSWER = frozenset({
    "multiple_correct", "no_correct", "ambiguous", "answer_leak",
})
REASONS_AFTER_ANSWER = REASONS_BEFORE_ANSWER | {"incorrect_key"}
ALL_REASONS = REASONS_AFTER_ANSWER

BONUS_WAGER_TIER = "gold"
BONUS_APPEAL_DEPOSIT_TIER = "gold"


def month_bounds(when: datetime) -> tuple[datetime, datetime]:
    """UTC calendar month: [start, next-month start)."""
    if when.tzinfo is None:
        raise ValueError("Month boundary requires an aware datetime")
    when = when.astimezone(timezone.utc)
    start = datetime(when.year, when.month, 1, tzinfo=timezone.utc)
    next_year = when.year + (when.month == 12)
    next_month = when.month % 12 + 1
    return start, datetime(next_year, next_month, 1, tzinfo=timezone.utc)


def appeal_fee_bronze(previous_monthly_appeals: int) -> int:
    """First 5 free; subsequent fees 1, 2, 4, 8... bronze, never refunded."""
    if isinstance(previous_monthly_appeals, bool) or previous_monthly_appeals < 0:
        raise ValueError("Invalid appeal count")
    return (1 << (previous_monthly_appeals - FREE_MONTHLY_APPEALS)
            if previous_monthly_appeals >= FREE_MONTHLY_APPEALS else 0)


def appeal_deposit_tier(question_number: int) -> str:
    """Question 1-5 bronze, 6-10 silver, 11-16 gold."""
    if isinstance(question_number, bool) or not 1 <= question_number <= 16:
        raise ValueError("Question number must be between 1 and 16")
    if question_number <= 5:
        return "bronze"
    if question_number <= 10:
        return "silver"
    return "gold"


def compensation_tier(question_number: int) -> str:
    """Only an upheld loss on the appealed question grants this reward."""
    if question_number == 16:
        return "platinum"
    return appeal_deposit_tier(question_number)


def validate_complaint(reasons: list[str], comment: str | None, *,
                       before_answer: bool) -> tuple[tuple[str, ...], str]:
    """Check boxes plus free text; either may independently express an appeal."""
    if not isinstance(reasons, list) or len(reasons) != len(set(reasons)):
        raise ValueError("Duplicate or malformed reasons")
    valid = REASONS_BEFORE_ANSWER if before_answer else REASONS_AFTER_ANSWER
    if any(not isinstance(reason, str) or reason not in valid for reason in reasons):
        raise ValueError("Invalid appeal reason for this answer stage")
    if comment is not None and not isinstance(comment, str):
        raise ValueError("Invalid comment")
    cleaned = (comment or "").strip()
    if len(cleaned) > MAX_COMMENT_LENGTH:
        raise ValueError("Comment exceeds 1000 characters")
    if not reasons and not cleaned:
        raise ValueError("Select a reason or provide an explanation")
    return tuple(reasons), cleaned


def should_auto_uphold(previous_upheld: int) -> bool:
    return previous_upheld >= 2


def permanent_block_reason(*, distinct_players: int, upheld: int) -> str | None:
    if upheld >= 1:
        return "confirmed_error"
    if distinct_players >= 3:
        return "complaint_threshold"
    return None


def loss_compensation_allowed(*, lost_question_id: str | None,
                              appealed_question_id: str) -> bool:
    return lost_question_id is not None and lost_question_id == appealed_question_id


def refund_deposit(*, upheld: bool, technical_unresolved: bool = False) -> bool:
    return upheld or technical_unresolved


def technical_deadline(submitted_at: datetime) -> datetime:
    if submitted_at.tzinfo is None:
        raise ValueError("Appeal timestamp must include timezone")
    return submitted_at + timedelta(days=RETRY_DAYS)


def must_close_technical(submitted_at: datetime, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("Current time must include timezone")
    return now >= technical_deadline(submitted_at)


def bonus_wager_refund(*, question_number: int, upheld: bool,
                       caused_loss: bool) -> bool:
    """Return the independent gold wager only for an upheld Q16 loss."""
    return question_number == 16 and upheld and caused_loss
