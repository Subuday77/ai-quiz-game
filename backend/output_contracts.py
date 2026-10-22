"""Explicit internal output contracts; no task/plugin registry."""

from enum import Enum

from quality_schema import QUALITY_SCHEMA_NAME, quality_review_schema
from quality_schema_v2 import QUALITY_V2_SCHEMA_NAME, quality_review_v2_schema
from quality_schema_v3 import QUALITY_V3_SCHEMA_NAME, quality_review_v3_schema
from question_transport_schema import QUIZ_SCHEMA_NAME, quiz_question_schema


class OutputContract(str, Enum):
    QUESTION = "question"
    QUALITY_REVIEW = "quality_review"
    QUALITY_REVIEW_V2 = "quality_review_v2"
    QUALITY_REVIEW_V3 = "quality_review_v3"


def transport_contract(contract: OutputContract) -> tuple[str, dict]:
    contract = OutputContract(contract)
    if contract == OutputContract.QUESTION:
        return QUIZ_SCHEMA_NAME, quiz_question_schema()
    if contract == OutputContract.QUALITY_REVIEW_V3:
        return QUALITY_V3_SCHEMA_NAME, quality_review_v3_schema()
    if contract == OutputContract.QUALITY_REVIEW_V2:
        return QUALITY_V2_SCHEMA_NAME, quality_review_v2_schema()
    return QUALITY_SCHEMA_NAME, quality_review_schema()
