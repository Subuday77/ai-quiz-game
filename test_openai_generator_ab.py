"""Blind, paired v1-v2 quiz-generator comparison (no reviewer calls).

Run from the project root in PyCharm:
    python test_openai_generator_ab.py
    python test_openai_generator_ab.py --all

Each A/B pair uses identical model/provider settings, language, category and
requested difficulty. The generated facts will naturally differ. The report
blinds the prompt versions so a human can compare quality independently.
The generator/reviewer production policy is never changed.
"""
import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
load_dotenv(ROOT / ".env")

from consts import LANGUAGE_DICT
from question_generator import create_question
from question_schema import QuizQuestion


# Fixed paired scenarios; both versions receive precisely the same input.
# First 8 cover the range encountered in the previous human game.
# --all additionally probes expert levels 8, 10, 12 and 15.
SCENARIOS = (
    (0, "Musical instruments"),
    (1, "Languages and linguistics"),
    (2, "Animals"),
    (3, "Transportation"),
    (4, "Inventions and discoveries"),
    (5, "Astronomy and space"),
    (6, "Television"),
    (7, "Traditions, customs, and world culture"),
    (8, "Visual arts"),
    (10, "World geography"),
    (12, "Classical music"),
    (15, "Ancient history"),
)


def variant_order(index: int) -> tuple[str, str]:
    """Alternate which model prompt gets label A to reduce order bias."""
    return ("v1", "v2") if index % 2 else ("v2", "v1")


def generate_pair(index: int, difficulty: int, category: str, language: str):
    rows = []
    for letter, version in zip("AB", variant_order(index)):
        row = {"pair": index, "variant": letter, "prompt_version": version,
               "difficulty": difficulty, "category": category, "language": language,
               "status": "error", "question": "", "answers": {},
               "correct_answer": "", "error": ""}
        try:
            raw = create_question(difficulty, language, category, prompt_version=version)
            question = QuizQuestion.model_validate(raw)
            if question.difficulty != difficulty:
                raise ValueError(f"Returned difficulty {question.difficulty}, expected {difficulty}")
            row.update(status="valid", question=question.question,
                       answers=question.answers, correct_answer=question.correct_answer)
        except (Exception) as exc:
            # The benchmark records transport, parsing and validation failures
            # separately. No automatic retries: one attempt per version.
            row["error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
        rows.append(row)
    return rows


def as_markdown(rows: list[dict]) -> str:
    lines = [
        "# Quiz generator A/B — blind human review",
        "",
        "Both variants use the same model, language, difficulty, category and settings.",
        "The prompt version is deliberately hidden until the answer key at the bottom.",
        "AI-provided correct answers are claims, not verified facts.",
        "Check level appropriateness, unfairly close alternatives, weak distractors,",
        "ambiguity, direct lexical leakage and whether the question is enjoyable.",
        "",
    ]
    for row in rows:
        if row["variant"] == "A":
            lines += [f"## Pair {row['pair']} — level {row['difficulty']} — {row['category']}", ""]
        lines.append(f"### Variant {row['variant']}")
        if row["status"] != "valid":
            lines += [f"Generation failed: {row['error']}", ""]
            continue
        lines += [row["question"], ""]
        for choice in "ABCD":
            lines.append(f"- {choice}. {row['answers'][choice]}")
        lines.append("")
    lines += ["## Answer key and prompt versions (reveal after assessment)", "",
              "| Pair | Variant | Prompt | Model-supplied answer | Status |",
              "|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['pair']} | {row['variant']} | {row['prompt_version']} "
                     f"| {row['correct_answer'] or '—'} | {row['status']} |")
    lines += ["", "The supplied correct-answer letters have not been fact-checked.",
              "Use qualitative judgment rather than the unreliable critic score.", ""]
    return "\n".join(lines)


def csv_row(row: dict) -> dict:
    return {**{key: value for key, value in row.items() if key != "answers"},
            **{f"answer_{key}": row["answers"].get(key, "") for key in "ABCD"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=8,
                        help="Number of paired scenarios (default: 8, max: 12)")
    parser.add_argument("--all", action="store_true", help="Run all 12 scenarios")
    parser.add_argument("--language", default="Russian", choices=list(LANGUAGE_DICT.values()))
    args = parser.parse_args()
    count = len(SCENARIOS) if args.all else args.pairs
    if not 1 <= count <= len(SCENARIOS):
        parser.error(f"--pairs must be from 1 to {len(SCENARIOS)}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = ROOT / f"generator_ab_{stamp}"
    rows = []
    print(f"GENERATOR A/B | {count} pairs | {args.language} | no critic | no retries")
    for i, (difficulty, category) in enumerate(SCENARIOS[:count], 1):
        print(f"[{i}/{count}] level {difficulty}: {category}")
        pair = generate_pair(i, difficulty, category, args.language)
        rows.extend(pair)
        for row in pair:
            print(f"  Variant {row['variant']}: {row['status']}"
                  + (f" ({row['error']})" if row["error"] else ""))
    jsonl_path = prefix.with_suffix(".jsonl")
    csv_path = prefix.with_suffix(".csv")
    md_path = prefix.with_suffix(".md")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["pair", "variant", "prompt_version", "difficulty", "category",
                  "language", "status", "question", "answer_A", "answer_B",
                  "answer_C", "answer_D", "correct_answer", "error"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_row(row) for row in rows)
    md_path.write_text(as_markdown(rows), encoding="utf-8")
    valid = sum(row["status"] == "valid" for row in rows)
    print(f"RESULT: {valid}/{len(rows)} valid responses (schema and level only)")
    print(f"BLIND REVIEW: {md_path}")
    print(f"CSV: {csv_path}")
    print(f"JSONL: {jsonl_path}")
    print("No factual-quality winner is declared automatically. Review the blind pairs.")


if __name__ == "__main__":
    main()
