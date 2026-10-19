"""LLM question generation (separate from game rules and terminal input)."""

import json
from typing import Any

from config import MAX_DIFFICULTY, MAX_OUTPUT_TOKENS, TEMPERATURE
from consts import QUESTION_PROMPT, client
from diagnostics import timed_stage


def create_question(complexity: int, language: str, category: str) -> dict[str, Any]:
    """Generate and parse one question; API schema validation/retries follow."""
    if not 0 <= complexity <= MAX_DIFFICULTY:
        raise ValueError(f"Complexity must be between 0 and {MAX_DIFFICULTY}")

    with timed_stage("provider_client_setup"):
        provider_client = client()
    response = provider_client.responses.create(
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
    with timed_stage("question_json_parse"):
        return json.loads(response.output_text)
