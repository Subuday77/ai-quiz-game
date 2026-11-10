"""Compare a reviewer_test JSONL against the draft labels; no remote API calls.

Usage: python tests/evaluate_critic_corpus.py reviewer_test_20261021_174852.jsonl
Scores only reviewed_draft examples. Human adjudication is still required.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "backend" / "skills" / "quiz_quality_review" / "examples.jsonl"


def load_jsonl(path):
    return [json.loads(s) for s in Path(path).read_text(encoding="utf-8").splitlines() if s.strip()]


def evaluate(results_path):
    gold = {(x["generator"], x["number"]): x for x in load_jsonl(CORPUS)}
    counts = Counter()
    seen = set()
    for item in load_jsonl(results_path):
        key = (item["generator"], item["number"])
        if key in seen:
            raise ValueError(f"Duplicate result: {key}")
        seen.add(key)
        if key not in gold:
            counts["unknown"] += 1
            continue
        target = gold[key]["gold"]
        if target["status"] == "provisional":
            counts["excluded_provisional"] += 1
            continue
        counts["scored"] += 1
        result = item.get("openai_review") or {}
        predicted = result.get("decision") if result.get("status") == "success" else None
        expected = target["decision"]
        if predicted is None:
            counts["unavailable"] += 1
        elif predicted == expected:
            counts["agreement"] += 1
        elif predicted == "accept" and expected != "accept":
            counts["false_accept"] += 1
        elif predicted != "accept" and expected == "accept":
            counts["false_reject"] += 1
        else:
            counts["other_disagreement"] += 1
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/evaluate_critic_corpus.py <reviewer_test.jsonl>")
    for metric, value in sorted(evaluate(sys.argv[1]).items()):
        print(f"{metric}: {value}")
