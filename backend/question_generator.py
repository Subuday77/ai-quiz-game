"""LLM question generation (separate from game rules and terminal input)."""

import json
import os
from typing import Any

from config import MAX_DIFFICULTY, MAX_OUTPUT_TOKENS, TEMPERATURE
from consts import client
from agents.generator import GENERATOR_PROMPT_V1, GENERATOR_PROMPT_V2
from diagnostics import timed_stage
from provider_result import completed_text


def select_generator_prompt(version: str | None = None) -> str:
    """Select version explicitly for A/B tests; default to legacy until validated."""
    version = version if version is not None else os.getenv("AI_GENERATOR_PROMPT_VERSION", "v1")
    if version == "v1":
        return GENERATOR_PROMPT_V1
    if version == "v2":
        return GENERATOR_PROMPT_V2
    raise ValueError("Invalid AI_GENERATOR_PROMPT_VERSION; use v1 or v2")


def create_question(complexity: int, language: str, category: str,
                    *, prompt_version: str | None = None) -> dict[str, Any]:
    """Generate and parse one question; API schema validation/retries follow."""
    if not 0 <= complexity <= MAX_DIFFICULTY:
        raise ValueError(f"Complexity must be between 0 and {MAX_DIFFICULTY}")

    instructions = select_generator_prompt(prompt_version)
    with timed_stage("provider_client_setup"):
        provider_client = client()
    response = provider_client.responses.create(
        instructions=instructions,
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
