"""Paid replay of EXACT Nov 2 defective game questions through current v5 critic.

Run from backend/:
    python replay_tagged_quality_nov2.py --list            # zero calls
    python replay_tagged_quality_nov2.py --case cannes_1976_false_shared_winner
    python replay_tagged_quality_nov2.py --all             # 3 paid critic calls

Uses check_tagged_quality() itself: no HTTP server, DB writes, question
generation or other model calls. Expected labels are never sent to AI.
"""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from time import perf_counter

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from quality_errors import QualityConfigurationError, QualityRegenerationError, QualityReviewFailure
from quality_schema import QualityMode
from tagging.contract import TaggedQuestion
from tagging.review import check_tagged_quality

FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "nov2_quality_regressions.json"


def verdict_matches_expected(expected: dict, verdict: dict) -> bool:
    if "premise" in expected and verdict.get("premise") != expected["premise"]:
        return False
    if "leakage" in expected and verdict.get("leakage") != expected["leakage"]:
        return False
    if "minimum_distractor_penalty" in expected and (
        verdict.get("distractor_penalty", -1) < expected["minimum_distractor_penalty"]
    ):
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--list", action="store_true", help="List tests without AI calls")
    choice.add_argument("--case", action="append", help="One case ID; repeat for selected tests")
    choice.add_argument("--all", action="store_true", help="Replay all 3 cases (billable)")
    args = parser.parse_args()
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    indexed = {case["id"]: case for case in cases}
    if args.list:
        for case in cases:
            print(case["id"], "| expected:", case["expected"])
        return
    if not os.getenv("OPENAI_API_KEY") and os.getenv("AI_QUALITY_PROVIDER", "openai") == "openai":
        parser.error("Set OPENAI_API_KEY in the root .env before paid replay")
    if args.case:
        unknown = set(args.case) - set(indexed)
        if unknown:
            parser.error("Unknown cases: " + ", ".join(sorted(unknown)))
        selected = list(dict.fromkeys(indexed[name]["id"] for name in args.case))
        targets = [indexed[name] for name in selected]
    else:
        targets = cases
    print(f"TAGGED V5 CRITIC REPLAY | {len(targets)} case(s), {len(targets)} billable critic calls")
    path = ROOT / ("tagged_critic_replay_nov2_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".jsonl")
    correct = 0
    try:
        timeout_seconds = float(os.getenv("AI_PROVIDER_TIMEOUT_SECONDS", "12"))
    except ValueError:
        parser.error("Invalid AI_PROVIDER_TIMEOUT_SECONDS")
    with path.open("w", encoding="utf-8") as dest:
        for index, case in enumerate(targets, 1):
            question = TaggedQuestion.model_validate(case["question"])
            failure = None
            started = perf_counter()
            try:
                check_tagged_quality(
                    question, language="Russian", category=case["category"],
                    requested_difficulty=question.difficulty, neighbors=[],
                    mode=QualityMode.AUDIT,
                    deadline=perf_counter() + max(30, 2 * timeout_seconds),
                    timeout_seconds=timeout_seconds,
                )
            except QualityRegenerationError:
                # Hard metadata failure in audit is expected for false premises.
                # The v5 report is attached before this exception is raised.
                pass
            except (QualityConfigurationError, QualityReviewFailure) as exc:
                failure = getattr(exc, "classification", type(exc).__name__)
            except Exception as exc:
                failure = type(exc).__name__
            report = question._review_report
            verdict = (report or {}).get("details", {}).get("verdict", {})
            diagnostic = (report or {}).get("details", {}).get("option_diagnostics", {})
            match = verdict_matches_expected(case["expected"], verdict) if verdict else False
            correct += int(match)
            row = {
                "case": case["id"], "expected": case["expected"],
                "match": match, "failure": failure,
                "premise": verdict.get("premise"),
                "facts": verdict.get("facts"), "options": verdict.get("options"),
                "fact_valid": verdict.get("fact_valid"),
                "leakage": verdict.get("leakage"),
                "issue_codes": verdict.get("issue_codes"),
                "distractor_penalty": verdict.get("distractor_penalty"),
                "diagnostics": diagnostic, "review": report,
                "elapsed_seconds": round(perf_counter() - started, 3),
            }
            dest.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            print(f"[{index}/{len(targets)}] {case['id']} | match={match} | "
                  f"premise={row['premise']} | leakage={row['leakage']} | "
                  f"distractors={row['distractor_penalty']} | "
                  f"fact_valid={row['fact_valid']} | error={failure or '-'}")
    print(f"EXPECTED LABELS MATCHED: {correct}/{len(targets)} (not a global accuracy estimate)")
    print("JSONL:", path)


if __name__ == "__main__":
    main()
