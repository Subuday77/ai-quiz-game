"""Summarize v3 audit logs collected from a real game, without API calls.

Start backend from project root in PowerShell:
  python -m uvicorn api:app --app-dir backend --reload --host 127.0.0.1 --port 8000 2>&1 | Tee-Object -FilePath audit_v3.log
Then, in a second terminal after the game:
  python analyze_quality_audit.py audit_v3.log
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_audits(path: Path):
    audits = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(line[start:])
        except (ValueError, TypeError):
            continue
        if event.get("event") == "quality_review_score" and event.get("contract") == "v3":
            audits.append(event)
    return audits


def summarize(audits):
    distribution = Counter(item["score"] for item in audits)
    critical = Counter(item["critical"] for item in audits)
    penalty_totals = Counter()
    for entry in audits:
        penalty_totals.update(entry["penalties"])
    thresholds = {
        number: sum(
            item["critical"] != "none" or item["score"] >= number
            for item in audits
        )
        for number in (2, 3, 4, 5)
    }
    return distribution, critical, penalty_totals, thresholds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path, help="Captured uvicorn log")
    args = parser.parse_args()
    if not args.log.is_file():
        parser.error(f"File not found: {args.log}")
    audits = read_audits(args.log)
    if not audits:
        print("No v3 audit events. Check AI_QUALITY_MODE=audit, AI_QUALITY_CONTRACT=v3 and the log file.")
        return
    distribution, critical, penalties, thresholds = summarize(audits)
    print(f"V3 AUDIT: {len(audits)} question reviews")
    print("Scores: " + ", ".join(f"{score}: {distribution[score]}" for score in sorted(distribution)))
    print("Critical: " + ", ".join(f"{kind}: {critical[kind]}"
                                   for kind in ("none", "regenerate", "escalate")))
    print("Penalty totals: " + ", ".join(f"{name}: {penalties[name]}"
                                        for name in ("distractors", "knowledge", "suitability", "wording")))
    for threshold, count in thresholds.items():
        print(f"If threshold >= {threshold}: {count}/{len(audits)} potential rejections "
              "(including critical faults; no real rejections performed)")
    path = args.log.with_name("audit_v3_scores.csv")
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "request_id", "attempt", "score", "critical", "distractors",
            "knowledge", "suitability", "wording", "difficulty", "issue_codes",
        ])
        writer.writeheader()
        for item in audits:
            writer.writerow({
                "request_id": item.get("request_id"),
                "attempt": item.get("attempt"),
                "score": item["score"],
                "critical": item["critical"],
                **{name: item["penalties"].get(name, 0) for name in
                   ("distractors", "knowledge", "suitability", "wording")},
                "difficulty": item.get("difficulty"),
                "issue_codes": ",".join(item.get("issue_codes", [])),
            })
    print(f"CSV: {path}")


if __name__ == "__main__":
    main()
