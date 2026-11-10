"""Explicit internal output contracts; no task/plugin registry."""

from enum import Enum

from quality_schema import QUALITY_SCHEMA_NAME, quality_review_schema
from quality_schema_v2 import QUALITY_V2_SCHEMA_NAME, quality_review_v2_schema
from quality_schema_v3 import QUALITY_V3_SCHEMA_NAME, quality_review_v3_schema
from question_transport_schema import QUIZ_SCHEMA_NAME, quiz_question_schema
from tagging.schemas import (
    TAGGED_QUESTION_SCHEMA_NAME, TAGGED_REVIEW_SCHEMA_NAME, TAG_EXTRACTION_SCHEMA_NAME,
    tagged_question_schema, tagged_review_schema, tag_extraction_schema,
    ARBITRATION_SCHEMA_NAME, arbitration_schema,
)


class OutputContract(str, Enum):
    QUESTION = "question"
    QUALITY_REVIEW = "quality_review"
    QUALITY_REVIEW_V2 = "quality_review_v2"
    QUALITY_REVIEW_V3 = "quality_review_v3"
    TAGGED_QUESTION = "tagged_question"
    TAGGED_REVIEW = "tagged_review"
    TAG_EXTRACTION = "tag_extraction"
    ARBITRATION = "arbitration"


def transport_contract(contract: OutputContract) -> tuple[str, dict]:
    contract = OutputContract(contract)
    if contract == OutputContract.QUESTION:
        return QUIZ_SCHEMA_NAME, quiz_question_schema()
    if contract == OutputContract.TAGGED_QUESTION:
        return TAGGED_QUESTION_SCHEMA_NAME, tagged_question_schema()
    if contract == OutputContract.TAGGED_REVIEW:
        return TAGGED_REVIEW_SCHEMA_NAME, tagged_review_schema()
    if contract == OutputContract.ARBITRATION:
        return ARBITRATION_SCHEMA_NAME, arbitration_schema()
    if contract == OutputContract.TAG_EXTRACTION:
        return TAG_EXTRACTION_SCHEMA_NAME, tag_extraction_schema()
    if contract == OutputContract.QUALITY_REVIEW_V3:
        return QUALITY_V3_SCHEMA_NAME, quality_review_v3_schema()
    if contract == OutputContract.QUALITY_REVIEW_V2:
        return QUALITY_V2_SCHEMA_NAME, quality_review_v2_schema()
    return QUALITY_SCHEMA_NAME, quality_review_schema()
