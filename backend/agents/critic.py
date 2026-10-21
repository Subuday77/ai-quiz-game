"""Specialized, read-only critic role: prompts are versioned outside Python code.

This module loads versioned reviewer instructions and preserves the QualityReview
contract. Orchestration, timeouts, retries and policy remain in quality_checker.py.
"""
from functools import lru_cache
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "quiz_quality_review" / "SKILL.md"
SKILL_VERSION = "1.1.0"


@lru_cache(maxsize=1)
def load_critic_skill() -> str:
    """Load the exact legacy critic instructions, without rewriting whitespace."""
    return SKILL_PATH.read_text(encoding="utf-8")


QUALITY_REVIEW_PROMPT = load_critic_skill()
