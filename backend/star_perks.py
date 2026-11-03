"""Pure, provider-independent rules for the planned consumable star economy.

No database access or AI call belongs here. FastAPI must enforce the same
rules transactionally under a user/round lock before any credit is spent.
"""
from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Literal

StarTier = Literal["bronze", "silver", "gold", "platinum"]
Choice = Literal["A", "B", "C", "D"]

STAR_UNIT_VALUES: dict[str, int] = {
    "bronze": 1,
    "silver": 10,
    "gold": 100,
}
REMOVE_ONE_PRICE_BRONZE = 2
REMOVE_TWO_PRICE_BRONZE = 3
RANDOM_CATEGORY_PRICE_SILVER = 1
CHOOSE_CATEGORY_PRICE_SILVER = 2
EXCLUDE_CATEGORY_PRICE_SILVER = 5
SKIP_QUESTION_PRICE_GOLD = 1
MAX_HIDDEN_CHOICES = 2
MAX_CATEGORY_CHANGES = 2
MAX_MANUAL_CATEGORY_CHANGES = 1
MIN_ALLOWED_CATEGORIES = 16
LEGACY_DISTRACTOR_SCORE = 50


class PerkUnavailable(ValueError):
    """A star perk is not allowed in the current question state."""


def exchange_quote(source: StarTier, target: StarTier, amount: int) -> int:
    """Amount of *whole* target stars for a free, reversible exchange.

    Platinum has no exchange rate. No fractions/rounding and no free credits
    from zero or negative exchanges.
    """
    if source not in STAR_UNIT_VALUES or target not in STAR_UNIT_VALUES:
        raise PerkUnavailable("Platinum cannot be exchanged")
    if source == target:
        raise PerkUnavailable("Exchange requires distinct tiers")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise PerkUnavailable("Exchange amount must be a positive integer")
    numerator = STAR_UNIT_VALUES[source] * amount
    divisor = STAR_UNIT_VALUES[target]
    if numerator % divisor:
        raise PerkUnavailable("Exchange requires a whole number of stars")
    return numerator // divisor


def remove_price(hidden_count: int, remove_count: int) -> int:
    """Discount applies only to removing two options in ONE action."""
    if hidden_count not in (0, 1, 2):
        raise PerkUnavailable("Invalid removed-choice state")
    if hidden_count == 2 or remove_count not in (1, 2):
        raise PerkUnavailable("No removal actions remain")
    if hidden_count + remove_count > MAX_HIDDEN_CHOICES:
        raise PerkUnavailable("Cannot remove more than two distractors")
    if remove_count == 2:
        if hidden_count:
            raise PerkUnavailable("Double removal requires a fresh question")
        return REMOVE_TWO_PRICE_BRONZE
    return REMOVE_ONE_PRICE_BRONZE


def normalized_plausibility(
    correct: Choice, scores: Mapping[str, int] | None,
) -> dict[str, int]:
    """Use the generator's one-time scores as-is, protecting the true key.

    Older pooled questions may have no scores; the neutral 50-point default
    only orders distractors, and does not trigger an extra LLM evaluation.
    """
    source = scores or {}
    result = {}
    for key in "ABCD":
        score = source.get(key, LEGACY_DISTRACTOR_SCORE)
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise PerkUnavailable("Invalid generator plausibility score")
        result[key] = score
    result[correct] = 100  # Server-owned key has absolute priority.
    return result


def choose_to_hide(
    correct: Choice,
    scores: Mapping[str, int] | None,
    already_hidden: Sequence[str] = (),
    *,
    count: int = 1,
    rng: random.Random | None = None,
) -> tuple[str, ...]:
    """Select lowest-scored WRONG answers; break score ties uniformly.

    The caller persists these returned keys on the attempt before returning a
    public answer list. Reload/retry must use the persisted result, not reroll.
    """
    if correct not in "ABCD" or len(correct) != 1:
        raise PerkUnavailable("Invalid correct key")
    hidden = tuple(already_hidden)
    if len(set(hidden)) != len(hidden) or any(key not in "ABCD" or key == correct for key in hidden):
        raise PerkUnavailable("Invalid hidden-choice state")
    remove_price(len(hidden), count)
    estimates = normalized_plausibility(correct, scores)
    randomizer = rng or random.SystemRandom()
    candidates = [key for key in "ABCD" if key != correct and key not in hidden]
    # Stable sort preserves tie order after fair shuffle.
    randomizer.shuffle(candidates)
    candidates.sort(key=lambda key: estimates[key])
    return tuple(candidates[:count])


def validate_allowed_categories(
    all_categories: Sequence[str], excluded: Sequence[str],
) -> tuple[str, ...]:
    """Apply one-game exclusions, preserving original catalog order."""
    if len(set(all_categories)) != len(all_categories):
        raise PerkUnavailable("Duplicate categories in the catalog")
    excluded_set = set(excluded)
    if len(excluded_set) != len(excluded) or not excluded_set.issubset(all_categories):
        raise PerkUnavailable("Invalid excluded categories")
    remaining = tuple(cat for cat in all_categories if cat not in excluded_set)
    if len(remaining) < MIN_ALLOWED_CATEGORIES:
        raise PerkUnavailable("At least 16 categories must remain")
    return remaining


def category_change_price(
    *, allowed_count: int, changes_used: int,
    manual_used: int, manual: bool,
) -> int:
    """Shared 2-per-question ceiling, manual sublimit 1, 16-category lockout."""
    if allowed_count <= MIN_ALLOWED_CATEGORIES:
        raise PerkUnavailable("Category changes require more than 16 allowed categories")
    if changes_used >= MAX_CATEGORY_CHANGES or changes_used < 0:
        raise PerkUnavailable("Category change limit reached")
    if manual_used < 0 or manual_used > MAX_MANUAL_CATEGORY_CHANGES:
        raise PerkUnavailable("Invalid manual category change state")
    if manual and manual_used >= MAX_MANUAL_CATEGORY_CHANGES:
        raise PerkUnavailable("Manual category change already used")
    return CHOOSE_CATEGORY_PRICE_SILVER if manual else RANDOM_CATEGORY_PRICE_SILVER
