"""Flag multi-part historical claims for independent verification.

The heuristic signals are NEVER proof of truth/falsity. A very small,
explicitly sourced contradiction registry can reject *exact* normalized
generator fact triples before paid review. Do not extrapolate from that
registry to other awards, years, or people.
"""
import re
import unicodedata

from .types import Finding

# Source: ACM's official laureate list (1972: Edsger W. Dijkstra).
# https://amturing.acm.org/laureates.cfm
# https://www.acm.org/binaries/content/assets/awards/turing50-program.pdf
# These are exact structured fact triples, NOT loose stem substring matching.
_KNOWN_FALSE_FACTS = {
    ("grace-hopper", "turing-award-year", "1972"): {
        "code": "acm_turing_1972_false_recipient",
        "reference": "https://amturing.acm.org/laureates.cfm",
        "verified_recipient": "edsger-w-dijkstra",
    },
}

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

_AWARD = re.compile(
    r"\b(?:преми\w*|наград\w*|лауреат\w*|awards?|prizes?|laureate\w*)\b",
    flags=re.UNICODE,
)
_YEAR = re.compile(r"\b(?:1[89]\d{2}|20\d{2})\b")
_AWARD_REASON = re.compile(
    r"\b(?:за\s+(?:вклад|создан\w*|разработ\w*|открыти\w*|достижен\w*)|"
    r"for\s+(?:contribution\w*|develop\w*|creat\w*|discover\w*))\b",
    flags=re.UNICODE,
)


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def known_false_triple(subject: str, relation: str, object_: str) -> dict | None:
    """Return a curated contradiction only on exact structured triple match."""
    return _KNOWN_FALSE_FACTS.get((subject, relation, object_))


def known_false_fact(question) -> dict | None:
    """Check a tagged candidate without guessing from names/wording."""
    fact = getattr(question, "fact", None)
    if fact is None:
        return None
    return known_false_triple(fact.subject, fact.relation, fact.object)


def inspect(question, settings) -> list[Finding]:
    stem = _normalized(question.question)
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

    # An award and a specific year are TWO independent assertions. If the
    # stem also claims a reason, it has at least THREE. This was missed in
    # the real 1972 Turing Award / COBOL / Hopper incident.
    award = _AWARD.search(stem)
    year = _YEAR.search(stem)
    reason = _AWARD_REASON.search(stem)
    if award and year:
        compound = bool(reason)
        findings.append(Finding(
            tool="fact",
            code="needs_source_verification:" + (
                "compound_award_year_reason" if compound else "award_year_attribution"
            ),
            severity=3,
            confidence=0.88 if compound else 0.82,
            evidence={
                "claim_parts": ["award", "year"] + (["reason"] if compound else []),
                "award_marker": award.group(0),
                "year": year.group(0),
                "keyed_option": question.correct_answer,
                "verification": "not_performed",
                "claim_truth": "unknown",
                "note": "confirm year, recipient and stated rationale independently",
            },
        ))

    verified = known_false_fact(question)
    if verified is not None:
        findings.append(Finding(
            tool="fact",
            code="known_contradiction:" + verified["code"],
            severity=3,
            confidence=1.0,
            evidence={
                "source": verified["reference"],
                "verified_recipient": verified["verified_recipient"],
                "claim_truth": "false",
                "verification": "curated_source",
                "note": "exact fact triple contradicts documented awardee",
            },
        ))
    return findings
