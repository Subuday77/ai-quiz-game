"""Stable portable transport schema; application constraints remain in QuizQuestion."""

import json
from copy import deepcopy

QUIZ_SCHEMA_NAME = "quiz_question_v1"
_SCHEMA = {
    "type": "object",
    "properties": {
        "difficulty": {"type": "integer"},
        "question": {"type": "string"},
        "answers": {
            "type": "object",
            "properties": {choice: {"type": "string"} for choice in ("A", "B", "C", "D")},
            "required": ["A", "B", "C", "D"],
            "additionalProperties": False,
        },
        "correct_answer": {"type": "string", "enum": ["A", "B", "C", "D"]},
    },
    "required": ["difficulty", "question", "answers", "correct_answer"],
    "additionalProperties": False,
}


def quiz_question_schema() -> dict:
    """A fresh copy prevents SDK/caller mutation from changing later requests."""
    return deepcopy(_SCHEMA)


def quiz_question_schema_json() -> str:
    return json.dumps(_SCHEMA, sort_keys=True, separators=(",", ":"))
