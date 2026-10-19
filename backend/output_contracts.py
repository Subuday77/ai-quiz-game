"""Two explicit internal output contracts; no task/plugin registry."""

from enum import Enum

from quality_schema import QUALITY_SCHEMA_NAME, quality_review_schema
from question_transport_schema import QUIZ_SCHEMA_NAME, quiz_question_schema


class OutputContract(str, Enum):
    QUESTION = "question"
    QUALITY_REVIEW = "quality_review"


def transport_contract(contract: OutputContract) -> tuple[str, dict]:
    contract = OutputContract(contract)
    if contract == OutputContract.QUESTION:
        return QUIZ_SCHEMA_NAME, quiz_question_schema()
    return QUALITY_SCHEMA_NAME, quality_review_schema()
