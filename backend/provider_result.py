"""Small internal completion contract; no provider SDK dependencies."""

from dataclasses import dataclass
from enum import Enum


class Completion(str, Enum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    BLOCKED = "blocked"
    TOOL_CALL = "tool_call"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderResult:
    output_text: str | None
    completion: Completion
    provider_reason: str | None


class ProviderCompletionError(Exception):
    """Retain completion metadata, but never include output/reason in error text."""

    def __init__(self, result: ProviderResult):
        self.completion = result.completion
        self.provider_reason = result.provider_reason
        super().__init__(f"Unusable provider completion: {result.completion.value}")


class TruncatedGenerationError(ProviderCompletionError):
    pass


class RefusedGenerationError(ProviderCompletionError):
    pass


class BlockedGenerationError(ProviderCompletionError):
    pass


class UnexpectedToolCallError(ProviderCompletionError):
    pass


class UnknownCompletionError(ProviderCompletionError):
    pass


class EmptyGenerationError(ProviderCompletionError):
    pass


def completed_text(result: ProviderResult) -> str:
    """Gate parsing on explicit completion, then require nonempty text."""
    failures = {
        Completion.TRUNCATED: TruncatedGenerationError,
        Completion.REFUSED: RefusedGenerationError,
        Completion.BLOCKED: BlockedGenerationError,
        Completion.TOOL_CALL: UnexpectedToolCallError,
        Completion.UNKNOWN: UnknownCompletionError,
    }
    if result.completion != Completion.COMPLETE:
        raise failures[result.completion](result)
    if not isinstance(result.output_text, str) or not result.output_text.strip():
        raise EmptyGenerationError(result)
    return result.output_text
