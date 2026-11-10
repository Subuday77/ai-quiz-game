"""Structured, non-authoritative signals produced by correctness tools."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Finding:
    tool: str
    code: str
    severity: int
    confidence: float
    evidence: dict
    # Model-independent relation evidence and actual question defect can
    # legitimately have very different certainty.
    relation_confidence: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)
