"""Standalone benchmark for optional critic v2. Does not modify game configuration.

Run from the project root:
    python test_openai_reviewer_v2.py
"""
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
load_dotenv(ROOT / ".env")

from agents.critic_v2 import QUALITY_REVIEW_V2_PROMPT
from quality_schema_v2 import QualityReviewV2, quality_review_v2_schema, decide_review
from question_schema import QuizQuestion

MODEL = "gpt-5.4-mini"
LIMIT = 30  # Set 1 for an initial smoke test.
TIMEOUT = 30
MAX_OUTPUT_TOKENS = 1200
REASONING_EFFORT = "low"
TEMPERATURE = 1.0
PRICE_INPUT = 0.75   # USD per million input tokens; update when tariffs change.
PRICE_OUTPUT = 4.50  # USD per million output tokens; includes reasoning tokens.

DATASETS = (
    ("lightning", "lightning_batch_20261021_162640.jsonl"),
    ("gpt-5.4-mini", "openai_batch_20261021_164811.jsonl"),
    ("gpt-5.4", "openai_batch_20261021_173838.jsonl"),
)

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=TIMEOUT, max_retries=0)



LEAK_CASES = (
    (4, "Human anatomy", "Какая кость образует лоб человека?",
     ["Теменная кость", "Лобная кость", "Скуловая кость", "Затылочная кость"], "B", "fail"),
    (2, "Board games", "В какой настольной игре игроки по очереди опускают фишки в вертикальную решётку, стараясь первыми собрать четыре в ряд?",
     ["Реверси", "Соедини четыре", "Скрэббл", "Морской бой"], "B", "fail"),
    (5, "Physics", "Какой физический закон описывает зависимость силы тока от напряжения и электрического сопротивления?",
     ["Закон Фарадея", "Закон Ампера", "Закон Ома", "Закон Кулона"], "C", "pass"),
    (8, "Literature", "Кто написал роман «Педро Парамо», герой которого приезжает в Комалу в поисках отца?",
     ["Карлос Фуэнтес", "Хуан Карлос Онетти", "Хуан Рульфо", "Хулио Кортасар"], "C", "pass"),
)


def load_leak_cases():
    items = []
    for number, (difficulty, category, stem, answers, key, expected) in enumerate(LEAK_CASES, 1):
        question = QuizQuestion.model_validate({
            "difficulty": difficulty, "question": stem,
            "answers": dict(zip("ABCD", answers)), "correct_answer": key,
        })
        items.append({
            "generator": "leak-regression", "number": number,
            "category": category, "question": question,
            "expected_leakage": expected,
        })
    return items


def load_candidates():
    items = []
    for generator, filename in DATASETS:
        path = ROOT / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing input: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            source = json.loads(line)
            question = QuizQuestion.model_validate(source["question"])
            items.append({
                "generator": generator,
                "number": source["number"],
                "category": source["category"],
                "question": question,
            })
    if len(items) != 30:
        raise ValueError(f"Expected 30 questions, found {len(items)}")
    return items


def review(item):
    question = item["question"]
    result = {
        "status": "failed", "decision": None, "issue_codes": [],
        "independent_answer": None, "rationale": None, "checks": None,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        "elapsed_seconds": 0.0, "error": None,
    }
    started = time.perf_counter()
    try:
        response = client.responses.create(
            model=MODEL,
            instructions=QUALITY_REVIEW_V2_PROMPT,
            input=json.dumps({
                "requested_difficulty": question.difficulty,
                "language": "Russian",
                "candidate": question.model_dump(),
            }, ensure_ascii=False),
            reasoning={"effort": REASONING_EFFORT},
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            text={"format": {
                "type": "json_schema",
                "name": "quality_review_v2",
                "strict": True,
                "schema": quality_review_v2_schema(),
            }},
        )
        if response.usage:
            result["input_tokens"] = response.usage.input_tokens
            result["output_tokens"] = response.usage.output_tokens
            result["cost_usd"] = round((
                response.usage.input_tokens * PRICE_INPUT +
                response.usage.output_tokens * PRICE_OUTPUT
            ) / 1_000_000, 8)
        if response.status != "completed" or not response.output_text:
            raise RuntimeError(f"Incomplete or empty response: {response.status}")
        report = QualityReviewV2.model_validate_json(response.output_text)
        decision, issues = decide_review(report, question.correct_answer)
        result.update({
            "status": "success",
            "decision": decision.value,
            "issue_codes": issues,
            "independent_answer": report.independent_answer,
            "rationale": report.rationale,
            "checks": {
                "facts": report.facts.value,
                "options": report.options.value,
                "language": report.language.value,
                "distractors": report.distractors.value,
                "leakage": report.leakage.value,
                "difficulty": report.difficulty.value,
            },
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:450]}"
    result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    return result


def main():
    leak_mode = '--leak-cases' in sys.argv[1:]
    candidates = load_leak_cases() if leak_mode else load_candidates()[:LIMIT]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "leak_review_v2" if leak_mode else "reviewer_v2"
    jsonl_path = ROOT / f"{prefix}_{stamp}.jsonl"
    csv_path = ROOT / f"{prefix}_{stamp}.csv"
    print(f"REVIEWER: {MODEL} | CONTRACT: v2 | REASONING: {REASONING_EFFORT}")
    print(f"QUESTIONS: {len(candidates)} | TEMPERATURE: {TEMPERATURE}")
    print("=" * 70)
    rows = []
    for index, item in enumerate(candidates, 1):
        q = item["question"]
        result = review(item)
        row = {
            "generator": item["generator"],
            "number": item["number"],
            "category": item["category"],
            "question": q.model_dump(),
            "openai_review": result,  # Compatible with evaluate_critic_corpus.py
            "expected_leakage": item.get("expected_leakage"),
        }
        rows.append(row)
        with jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\n[{index}/{len(candidates)}] {item['generator']} #{item['number']}")
        print(f"Q: {q.question}")
        print(f"{result['status']} ({result['elapsed_seconds']}s) | {result['decision']}")
        print(f"CHECKS: {result['checks']}")
        if leak_mode:
            observed = (result["checks"] or {}).get("leakage")
            expected = item["expected_leakage"]
            print(f"EXPECTED LEAKAGE: {expected} | OBSERVED: {observed}")
        print(f"ISSUES: {result['issue_codes']}")
        print(f"RATIONALE: {result['rationale']}")
        if result["error"]:
            print(f"ERROR: {result['error']}")
        print(f"COST: ${result['cost_usd']:.6f}")

    columns = ["generator", "number", "category", "question", "correct_answer",
               "status", "decision", "issues", "independent_answer", "facts",
               "options", "language", "distractors", "leakage", "difficulty",
               "rationale", "elapsed_seconds", "input_tokens", "output_tokens",
               "cost_usd", "error", "expected_leakage"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            q = row["question"]
            r = row["openai_review"]
            checks = r["checks"] or {}
            writer.writerow({
                "generator": row["generator"], "number": row["number"],
                "category": row["category"], "question": q["question"],
                "correct_answer": q["correct_answer"],
                "status": r["status"], "decision": r["decision"],
                "issues": ",".join(r["issue_codes"]),
                "independent_answer": r["independent_answer"],
                **{key: checks.get(key) for key in
                   ("facts", "options", "language", "distractors", "leakage", "difficulty")},
                "rationale": r["rationale"], "elapsed_seconds": r["elapsed_seconds"],
                "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                "cost_usd": r["cost_usd"], "error": r["error"],
                "expected_leakage": row["expected_leakage"],
            })

    counts = Counter(r["openai_review"]["decision"] or "failed" for r in rows)
    print("\n" + "=" * 70)
    print("SUMMARY")
    print(f"Successful: {sum(r['openai_review']['status'] == 'success' for r in rows)}/{len(rows)}")
    for key in ("accept", "regenerate", "escalate", "failed"):
        print(f"{key}: {counts[key]}")
    print(f"Estimated cost: ${sum(r['openai_review']['cost_usd'] for r in rows):.6f}")
    if leak_mode:
        matched = sum((r["openai_review"]["checks"] or {}).get("leakage") == r["expected_leakage"] for r in rows)
        print(f"LEAKAGE MATCH: {matched}/{len(rows)}")
    print(f"JSONL: {jsonl_path}\nCSV: {csv_path}")


if __name__ == "__main__":
    main()
