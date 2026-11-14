"""Server-only DB-first, reputational sampling and community review policy."""
import math
import os


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}")
    return number


def question_source_order() -> str:
    value = os.getenv("QUIZ_DB_FIRST_GENERATION", "false").strip().lower()
    if value not in ("true", "false"):
        raise ValueError("QUIZ_DB_FIRST_GENERATION must be true or false")
    return "pool_first" if value == "true" else "generate_first"


def community_vote_threshold() -> int:
    return _integer("QUIZ_QUESTION_RATING_REVIEW_THRESHOLD", -15, -100000, -1)


def first_acquittal_score() -> int:
    return _integer("QUIZ_QUESTION_FIRST_ACQUITTAL_SCORE", -5, -100000, 100000)


def second_acquittal_score() -> int:
    return _integer("QUIZ_QUESTION_SECOND_ACQUITTAL_SCORE", -10, -100000, 100000)


def max_referrals() -> int:
    # The specification defines two independent acquittals then an auto-ban.
    return _integer("QUIZ_QUESTION_MAX_REFERRALS", 3, 1, 3)


def _finite_float(name: str, default: float, *, positive: bool) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError:
        raise ValueError(f"{name} must be a real number") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite positive number" if positive
                         else f"{name} must be a finite number")
    return value


def rating_center() -> float:
    return _finite_float("QUIZ_DB_FIRST_RATING_CENTER", 0.0, positive=False)


def rating_scale() -> float:
    return _finite_float("QUIZ_DB_FIRST_RATING_SCALE", 10.0, positive=True)


def admission_probability(rating: int, *, center: float, scale: float) -> float:
    """Numerically stable sigmoid; probability of *admission*, not selection."""
    z = (rating - center) / scale
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 709)))
    t = math.exp(max(z, -709))
    return t / (1.0 + t)
