"""One shadow-only quality-skill runner and a future arbitration plan.

Runs entirely locally: 0 provider calls, 0 publication effects, and no
mutation of the candidate or the v5 critic verdict.
"""
from time import perf_counter

from diagnostics import debug_event, elapsed_ms
from . import fact_tool, lexical_tool, taxonomy_tool
from .config import SkillSettings


def evaluate_shadow(question, *, requested_difficulty: int,
                    settings: SkillSettings | None = None) -> dict:
    """Return deterministic per-tool findings plus a NON-ACTIVE arbitration plan."""
    start = perf_counter()
    config = settings or SkillSettings.from_env()
    if config.mode == "off":
        return {
            "mode": "off", "would_arbitrate": False,
            "arbiter_called": False, "findings": [], "tool_scores": {},
        }

    checks = (
        ("lexical", lexical_tool.inspect, config.lexical_penalty_weight),
        ("taxonomy", taxonomy_tool.inspect, config.taxonomy_penalty_weight),
        ("fact", fact_tool.inspect, config.fact_penalty_weight),
    )
    all_findings = []
    tool_scores = {}
    for name, check, weight in checks:
        found = check(question, config)
        all_findings.extend(found)
        # Highest signal per tool: repeated words must not stack an
        # arbitrary number of penalties from a single underlying defect.
        score = max((round(weight * f.severity / 3 * f.confidence, 3)
                     for f in found), default=0.0)
        tool_scores[name] = score
        debug_event(
            "quality_skill_shadow_tool",
            mode="shadow", tool=name,
            risk_score=score,
            findings=[f.to_dict() for f in found],
        )
    arbitration = [
        f.to_dict() for f in all_findings
        if f.severity >= config.arbitration_min_severity
        and f.confidence >= config.arbitration_min_confidence
    ]
    result = {
        "mode": "shadow",
        "requested_difficulty": requested_difficulty,
        "risk_score": round(sum(tool_scores.values()), 3),
        "tool_scores": tool_scores,
        "findings": [f.to_dict() for f in all_findings],
        "arbitration_priority": max((f.severity for f in all_findings), default=0),
        "arbitration_reasons": [f["code"] for f in arbitration],
        "would_arbitrate": bool(arbitration),
        "arbiter_called": False,
        "decision": "observe_only",
        "duration_ms": elapsed_ms(start),
    }
    debug_event("quality_skill_shadow", **result)
    return result
