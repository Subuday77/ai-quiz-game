"""Read-only inspection of quiz protections in the ACTIVE process environment.

Never print secrets, request payloads, passwords, URLs, or player history.
Use on VPS: docker compose exec -T backend python -m scripts.audit_runtime_guards --strict
The check cannot prove tag backfill completeness or firewall/TLS readiness.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Finding:
    key: str
    level: str
    description: str


# effective defaults come from the runtime modules, not deployment templates.
EXPECTED = {
    "AI_TAG_DEDUP_ENABLED": ("true", "false", "User-wide tagged fact dedup"),
    "AI_QUALITY_MODE": ("enforce", "off", "Blocking factual/editorial review"),
    "AI_LOCAL_LEAK_MODE": ("enforce", "enforce", "Literal-answer leak gate"),
    "QUIZ_QUALITY_ARBITER_MODE": ("on", "off", "Terra arbitration"),
    "AI_STRUCTURED_OUTPUT_MODE": ("strict", "off", "Generator native JSON schema"),
    "AI_QUALITY_CONTRACT": ("v2", "v1", "Legacy critic contract"),
    "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": ("true", "true", "Approved pool fallback"),
    "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": ("true", "true", "Strict too-easy-only reserve"),
    "AI_GENERATION_DEBUG": ("false", "false", "No sensitive generation debug"),
    "QUIZ_HELPER_DEBUG": ("false", "false", "No sensitive helper debug"),
    "LOCALIZATION_RESET_ENABLED": ("false", "false", "No localization-reset dev endpoint"),
    "QUIZ_QA_APPEAL_CREDITS_ENABLED": ("false", "false", "No test appeal credits"),
}


def audit(values: Mapping[str, str]) -> list[Finding]:
    found: list[Finding] = []
    for key, (expected, default, label) in EXPECTED.items():
        effective = values.get(key, default).strip().lower()
        source = "explicit" if key in values else "default"
        if effective == expected:
            found.append(Finding(key, "PASS", f"{label}; {source}"))
        else:
            found.append(Finding(key, "FAIL", f"{label}; effective={effective!r} ({source}), expected={expected!r}"))
    reviewer_mode = values.get("AI_QUALITY_STRUCTURED_OUTPUT_MODE",
                               values.get("AI_STRUCTURED_OUTPUT_MODE", "off")).strip().lower()
    found.append(Finding(
        "AI_QUALITY_STRUCTURED_OUTPUT_MODE", "PASS" if reviewer_mode == "strict" else "FAIL",
        "Reviewer native JSON schema; " + ("strict" if reviewer_mode == "strict" else "not strict"),
    ))
    main_model = values.get("OPENAI_MODEL_ID", "").strip()
    review_model = values.get("AI_QUALITY_MODEL_ID", "").strip()
    found.append(Finding(
        "AI_QUALITY_MODEL_ID", "PASS" if main_model and review_model and main_model != review_model else "FAIL",
        "Independent reviewer model " +
        ("is configured" if main_model and review_model and main_model != review_model
         else "is absent, main model missing, or equal to generator"),
    ))
    reviewer_provider = values.get("AI_QUALITY_PROVIDER", values.get("LLM_PROVIDER", "openai")).strip().lower()
    found.append(Finding(
        "AI_QUALITY_PROVIDER", "PASS" if reviewer_provider == "openai" else "WARN",
        "Reviewer provider matches approved OpenAI profile" if reviewer_provider == "openai"
        else "Reviewer provider differs from the approved OpenAI profile",
    ))
    keddy = values.get("QUIZ_COMPLAINT_KEDDY_MODEL_ID", "").strip()
    legacy = values.get("QUIZ_COMPLAINT_CADDY_MODEL_ID", "").strip()
    judge = values.get("QUIZ_COMPLAINT_JUDGE_MODEL_ID", "").strip()
    found.append(Finding(
        "QUIZ_COMPLAINT_KEDDY_MODEL_ID", "PASS" if keddy else ("WARN" if legacy else "FAIL"),
        "Keddy is configured" if keddy else
        ("Legacy CADDY alias only; migrate to KEDDY" if legacy else "Keddy model missing"),
    ))
    found.append(Finding(
        "QUIZ_COMPLAINT_JUDGE_MODEL_ID",
        "PASS" if judge and main_model and judge != main_model else "FAIL",
        "Judge differs from generator" if judge and main_model and judge != main_model
        else "Judge missing, generator missing, or conflicts with generator",
    ))
    allowed_origins = values.get("AUTH_ALLOWED_ORIGINS", "").strip()
    found.append(Finding(
        "AUTH_ALLOWED_ORIGINS",
        "PASS" if allowed_origins and "YOUR_DOMAIN" not in allowed_origins else "FAIL",
        "Origin allowlist configured" if allowed_origins and "YOUR_DOMAIN" not in allowed_origins
        else "Origin allowlist missing or still placeholder",
    ))
    secure = values.get("AUTH_COOKIE_SECURE", "true").strip().lower()
    if secure == "true":
        found.append(Finding("AUTH_COOKIE_SECURE", "PASS", "Secure session cookie"))
    elif secure == "false" and all(
        item.strip() in {"http://localhost:8080", "http://127.0.0.1:8080"}
        for item in allowed_origins.split(",") if item.strip()
    ) and allowed_origins:
        found.append(Finding("AUTH_COOKIE_SECURE", "WARN",
                             "HTTP cookie permitted for PRIVATE SSH-tunnel staging ONLY"))
    else:
        found.append(Finding("AUTH_COOKIE_SECURE", "FAIL",
                             "Insecure cookie outside approved private SSH-tunnel origin"))
    found.append(Finding("QUIZ_QUALITY_SKILL_MODE", "INFO",
                         "Shadow checks are observe-only; not a blocking gate"))
    found.append(Finding("QUIZ_DB_FIRST_GENERATION", "INFO",
                         "DB First is a separate rollout choice, not a safety gate"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only effective quiz guardrail audit")
    parser.add_argument("--strict", action="store_true",
                        help="Exit nonzero if a blocking protection is weak/missing")
    args = parser.parse_args()
    results = audit(os.environ)
    for result in results:
        print(f"{result.level:4} {result.key}: {result.description}")
    failures = sum(f.level == "FAIL" for f in results)
    print(f"Audit: {failures} failed protection checks. No secrets or DB data inspected.")
    print("Separately require: python -m tagging.backfill --dry-run => 0 missing.")
    return 2 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
