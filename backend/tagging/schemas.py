"""Portable JSON Schema contracts for tagged generation and critic verification."""
from copy import deepcopy
from question_transport_schema import quiz_question_schema
from quality_schema_v3 import quality_review_v3_schema

TAGGED_QUESTION_SCHEMA_NAME = "quiz_question_tagged_v2"
TAGGED_REVIEW_SCHEMA_NAME = "quiz_tagged_review_v4"
TAG_EXTRACTION_SCHEMA_NAME = "quiz_fact_extraction_v1"

_TAG_CLOUD = {
    "type": "object",
    "properties": {
        "entity": {"type": "array", "items": {"type": "string"}},
        "concept": {"type": "array", "items": {"type": "string"}},
        "keyword": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["entity", "concept", "keyword"],
    "additionalProperties": False,
}
_FACT = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in ("subject", "relation", "object")},
    "required": ["subject", "relation", "object"],
    "additionalProperties": False,
}


def tagged_question_schema() -> dict:
    schema = quiz_question_schema()
    schema["properties"]["tags"] = deepcopy(_TAG_CLOUD)
    schema["properties"]["fact"] = deepcopy(_FACT)
    schema["required"] += ["tags", "fact"]
    return schema


def tag_extraction_schema() -> dict:
    return {
        "type": "object",
        "properties": {"tags": deepcopy(_TAG_CLOUD), "fact": deepcopy(_FACT)},
        "required": ["tags", "fact"],
        "additionalProperties": False,
    }


def tagged_review_schema() -> dict:
    schema = quality_review_v3_schema()
    schema["properties"].update({
        "tags_valid": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "fact_valid": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "semantic_duplicate": {"type": "boolean"},
        "duplicate_match": {"type": "string"},
    })
    schema["required"] += ["tags_valid", "fact_valid", "semantic_duplicate", "duplicate_match"]
    return schema
