"""LLM question generation (separate from game rules and terminal input)."""

import json
from typing import Any

from config import MAX_DIFFICULTY, MAX_OUTPUT_TOKENS, TEMPERATURE
from consts import QUESTION_PROMPT, client


def create_question(complexity: int, language: str, category: str) -> dict[str, Any]:
    """Generate one question. Schema validation/retries follow in roadmap phase 1."""
    if not 0 <= complexity <= MAX_DIFFICULTY:
        raise ValueError(f"Complexity must be between 0 and {MAX_DIFFICULTY}")

    response = client().responses.create(
        instructions=QUESTION_PROMPT,
        input=(
            f"Generate a question with difficulty level {complexity}. "
            f"Topic: {category}. "
            f"Use {language} to choose a language of question."
        ),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        reasoning={"effort": "none"},
        temperature=TEMPERATURE,
    )
    return json.loads(response.output_text)
