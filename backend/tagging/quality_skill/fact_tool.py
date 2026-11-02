"""Identify claims that need independent evidence, not invent fact-checking.

Without outside sources a shadow tool can only nominate a claim for
verification. Words such as 'first', 'only' or 'fewest of all' are risk
markers; their presence is NEVER itself evidence that the question is false.
"""
import re
import unicodedata

from .types import Finding

_PATTERNS = (
    ("superlative_comparison", 3, 0.68, (
        r"\b(?:меньше\s+всего|больше\s+всего|среди\s+всех|наименьш\w*|"
        r"наибольш\w*|сам\w*\s+высок\w*|fewest|least|most\s+points|"
        r"among\s+all|highest|lowest)\b")),
    ("exclusive_claim", 3, 0.68, (
        r"\b(?:единственн\w*|исключительн\w*|only|sole|unique)\b")),
    ("first_or_last_claim", 2, 0.62, (
        r"\b(?:перв\w*|последн\w*|first|last|earliest)\b")),
    ("compound_historical_claim", 2, 0.60, (
        r"\b(?:разделив\w*\s+(?:награду|преми\w*)|совместно\s+получив\w*|"
        r"shared\s+(?:an?\s+)?(?:award|prize))\b")),
)


def inspect(question, settings) -> list[Finding]:
    stem = unicodedata.normalize("NFKC", question.question).casefold().replace("ё", "е")
    findings: list[Finding] = []
    for code, severity, confidence, pattern in _PATTERNS:
        match = re.search(pattern, stem, flags=re.UNICODE)
        if match:
            findings.append(Finding(
                tool="fact",
                code="needs_source_verification:" + code,
                severity=severity,
                confidence=confidence,
                evidence={
                    "trigger": match.group(0),
                    "verification": "not_performed",
                    "claim_truth": "unknown",
                    "note": "source-check or arbitration required before a factual verdict",
                },
            ))
    return findings
