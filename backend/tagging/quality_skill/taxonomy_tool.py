"""Detect option hierarchy, not automatically multiple correct answers.

Curated relationships are verifiable relations, not automatic verdicts about
the full stem. Generic word-containment is only a weak review candidate.
"""
import re
import unicodedata

from .types import Finding

# Explicit, narrowly curated parent-child identities. Extend with reviewed
# source-backed aliases rather than asking an LLM to invent a taxonomy.
_PARENTS = {
    "мюзелер": "верджинел",
    "мюзилер": "верджинел",
    "muselaar": "virginal",
    "бенгальский тигр": "тигр",
    "bengal tiger": "tiger",
    "золотистый ретривер": "собака",
    "golden retriever": "dog",
}


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W\d_]+", value, re.UNICODE))


def inspect(question, settings) -> list[Finding]:
    choices = {key: _norm(text) for key, text in question.answers.items()}
    out: list[Finding] = []
    for parent_key, parent in choices.items():
        for child_key, child in choices.items():
            if parent_key == child_key or parent == child or not parent or not child:
                continue
            known = _PARENTS.get(child) == parent
            # An answer that modifies another option may be a subtype,
            # e.g. 'Бенгальский тигр' versus 'тигр'. Equally it may be
            # merely a homonym; this weak signal is never a hard verdict.
            contains = (len(parent) >= 4
                        and len(child.split()) > len(parent.split())
                        and re.search(r"(?<!\w)" + re.escape(parent) + r"(?!\w)", child))
            if not known and not contains:
                continue
            out.append(Finding(
                tool="taxonomy",
                code="curated_subtype_overlap" if known else "possible_subtype_overlap",
                severity=3 if known else 1,
                # This is confidence that both options CAN overlap, not
                # the probability the full stem truly has two correct answers.
                confidence=0.65 if known else 0.35,
                relation_confidence=0.98 if known else 0.35,
                evidence={
                    "parent_option": parent_key,
                    "child_option": child_key,
                    "parent": parent,
                    "child": child,
                    "relation": "is-a",
                    "source": "curated" if known else "label_containment",
                    "applicability_to_stem": "unverified",
                },
            ))
    return out
