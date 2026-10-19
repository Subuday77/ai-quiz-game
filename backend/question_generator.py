"""LLM question generation (separate from game rules and terminal input)."""

import json
from typing import Any

from config import MAX_DIFFICULTY, MAX_OUTPUT_TOKENS, TEMPERATURE
from consts import QUESTION_PROMPT, client
from diagnostics import timed_stage
from provider_result import completed_text


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
    text = completed_text(response)
    with timed_stage("question_json_parse"):
        return json.loads(normalize_output(text))


def normalize_output(text: str) -> str:
    """Remove optional Markdown fences only after completion has been checked."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
