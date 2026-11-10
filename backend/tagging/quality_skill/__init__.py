"""Shadow-only question correctness tools.

The current v5 reviewer remains the publication authority. The skill never
changes a question, makes an LLM request, or raises a quality rejection.
"""
from .engine import evaluate_shadow

__all__ = ["evaluate_shadow"]
