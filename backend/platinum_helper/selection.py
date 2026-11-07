"""Choose a believable wrong answer from the generator's stored estimates.

The first KNOW/MISTAKEN/UNSURE draw still comes from the helper's skills.
A MISTAKEN draw may abstain when every visible wrong choice is implausible.
No extra AI scoring and no change to question-review records.
"""
from __future__ import annotations

from random import Random
from typing import Mapping, Sequence

from star_perks import PerkUnavailable, normalized_plausibility

from platinum_helper.settings import HelperSettings


def cautious_threshold(difficulty: int) -> int:
    """Thresholds calibrated on the 224-question November 2026 sample."""
    if not 0 <= difficulty <= 15:
        raise ValueError("Invalid question difficulty")
    settings = HelperSettings.from_env()
    return (settings.threshold_level_0 if difficulty == 0 else
            settings.threshold_levels_1_3 if difficulty <= 3 else
            settings.threshold_levels_4_15)


def best_visible_wrong(
    *,
    correct_key: str,
    difficulty: int,
    scores: Mapping[str, int] | None,
    hidden: Sequence[str] = (),
    rng: Random | None = None,
) -> str | None:
    """Return a plausible, currently visible wrong key, or abstain.

    Unlike the bronze hint, Platinum must abstain if a legacy question has
    no complete scoring map. It would otherwise recommend random nonsense.
    Corrupt scores also abstain rather than triggering new AI evaluation.
    A tie is resolved by the caller's RNG; the result is then frozen in DB.
    """
    if not isinstance(scores, Mapping) or any(key not in scores for key in "ABCD"):
        return None
    try:
        estimates = normalized_plausibility(correct_key, scores)
    except (PerkUnavailable, TypeError, ValueError):
        return None
    options = [key for key in "ABCD" if key != correct_key and key not in hidden]
    if not options:
        return None
    high = max(estimates[key] for key in options)
    if high < cautious_threshold(difficulty):
        return None
    tied = [key for key in options if estimates[key] == high]
    return rng.choice(tied) if rng is not None else tied[0]
