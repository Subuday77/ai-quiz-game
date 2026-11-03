"""Portable JSON Schema contracts for tagged generation and critic verification."""
from copy import deepcopy
from question_transport_schema import quiz_question_schema
from quality_schema_v3 import quality_review_v3_schema

TAGGED_QUESTION_SCHEMA_NAME = "quiz_question_tagged_v2"
TAGGED_REVIEW_SCHEMA_NAME = "quiz_tagged_review_v5"
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

# Structured evidence from the SAME tagged critic call, not a second model.
# V5 appends mandatory fields while retaining all v3/v4 review fields.
_OPTION_ASSESSMENT = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["yes", "no", "uncertain"]},
        "rationale": {"type": "string"},
    },
    "required": ["status", "rationale"],
    "additionalProperties": False,
}
_OPTION_ASSESSMENTS = {
    "type": "object",
    "properties": {key: deepcopy(_OPTION_ASSESSMENT) for key in "ABCD"},
    "required": list("ABCD"),
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
        "premise": {"type": "string", "enum": ["valid", "invalid", "uncertain"]},
        "option_assessments": deepcopy(_OPTION_ASSESSMENTS),
    })
    schema["required"] += [
        "tags_valid", "fact_valid", "semantic_duplicate", "duplicate_match",
        "premise", "option_assessments",
    ]
    return schema

# Terra's separate, independent arbitration contract. Every option and each
# material clause in the stem must be examined independently of the generator
# and primary critic. Provider-native strict JSON Schema supports this shape.
ARBITRATION_SCHEMA_NAME = "quiz_quality_arbitration_v1"

def arbitration_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "premise": {"type": "string", "enum": ["valid", "invalid", "uncertain"]},
            "choices": {
                "type": "object",
                "properties": {letter: {"type": "string", "enum": [
                    "yes", "no", "uncertain"]} for letter in "ABCD"},
                "required": list("ABCD"),
                "additionalProperties": False,
            },
            "difficulty": {"type": "string", "enum": [
                "appropriate", "too_easy", "too_hard"]},
            "answer_leak": {"type": "boolean"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "verdict": {"type": "string", "enum": [
                            "supported", "false", "uncertain"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["claim", "verdict", "reason"],
                    "additionalProperties": False,
                },
            },
            "rationale": {"type": "string"},
        },
        "required": ["premise", "choices", "difficulty", "answer_leak",
                     "claims", "rationale"],
        "additionalProperties": False,
    }
