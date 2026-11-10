"""30 approved PostgreSQL questions, all 11 interface languages, GPT-5.4.

Run in PyCharm, from backend:
  python -m scripts.helper_speech_regression_v6_30           # FREE preview
  python -m scripts.helper_speech_regression_v6_30 --execute # paid 30 calls

No game purchases, wallet updates or DB writes. Plan is version-frozen and
resumable; existing completed calls are not repeated. Budget is estimated.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import load_dotenv
from openai import OpenAI

from platinum_helper.settings import HelperSettings
from scripts import helper_live_regression as qa

VERSION = "platinum-speech-v6-multilingual-30-v1"
DIR = qa.BACKEND / "qa_reports" / "platinum_helper_speech_v6_30"
OUTCOMES = ("KNOW", "MISTAKEN", "UNSURE")
FRAMES = ("DIRECT", "PERSONAL", "CASUAL")
CODES = {"german": "de", "de": "de", "russian": "ru", "ru": "ru",
         "english": "en", "en": "en"}
# The first nine guarantee 3 question languages x 3 outcomes, like V6 smoke.
BASE = (
    ("German", "ru", "KNOW"),
    ("German", "en", "MISTAKEN"),
    ("German", "de", "UNSURE"),
    ("Russian", "de", "KNOW"),
    ("Russian", "en", "MISTAKEN"),
    ("Russian", "ru", "UNSURE"),
    ("English", "ru", "KNOW"),
    ("English", "de", "MISTAKEN"),
    ("English", "en", "UNSURE"),
)
# Extra UI locales (2-3 each): European, right-to-left, and CJK scripts.
MORE_UI = (
    "fr", "es", "it", "pt", "he", "ar", "ja", "zh",
    "fr", "es", "it", "pt", "he", "ar", "ja", "zh",
    "fr", "es", "it", "pt", "he",
)
SPECS = BASE + tuple(
    (("German", "Russian", "English")[i % 3], ui, OUTCOMES[i % 3])
    for i, ui in enumerate(MORE_UI)
)
assert len(SPECS) == 30


def code(language: str) -> str:
    return CODES.get(language.strip().casefold(), "")


def build_plan(questions: list[dict], seed: int, model: str) -> dict:
    """Only real distinct approved questions; no fabricated German questions."""
    pool = [q for q in questions if code(q["language"])]
    random.Random(seed).shuffle(pool)
    used: set[str] = set()
    slots: list[dict | None] = [None] * 30
    # First reserve all 9 guaranteed question-language/outcome combinations;
    # MISTAKEN needs plausibility scores and is the most constrained mode.
    order = [
        i for mode in ("MISTAKEN", "KNOW", "UNSURE")
        for i in range(9) if SPECS[i][2] == mode
    ] + [
        i for mode in ("MISTAKEN", "KNOW", "UNSURE")
        for i in range(9, 30) if SPECS[i][2] == mode
    ]
    for index in order:
        preferred, ui, outcome = SPECS[index]
        languages = [code(preferred)]
        if index >= 9:
            languages += [x for x in ("de", "ru", "en") if x not in languages]
        row = None
        for lang in languages:
            for q in pool:
                if q["question_id"] in used or code(q["language"]) != lang:
                    continue
                row = qa.scenario(q, outcome, index, seed)
                if row:
                    break
            if row:
                break
        if row is None:
            raise RuntimeError(
                f"S{index+1:03} ({outcome}, question {preferred}): "
                "not enough approved eligible questions / plausible distractors. "
                "No fabricated questions or paid API calls."
            )
        used.add(row["question_id"])
        row["speech_language"] = ui
        # Balanced QA-only overrides, never production correctness signals.
        if index < 9:
            row["presentation_style"] = (
                "DIRECT", "PERSONAL", "CASUAL", "PERSONAL", "CASUAL",
                "DIRECT", "CASUAL", "DIRECT", "PERSONAL",
            )[index]
            if outcome != "UNSURE":
                row["delivery_style"] = (
                    "CONFIDENT" if index in (0, 1, 4) else "TENTATIVE"
                )
        else:
            extra = index - 9
            row["presentation_style"] = FRAMES[
                (extra // 3 + extra % 3) % 3
            ]
            if outcome != "UNSURE":
                row["delivery_style"] = (
                    "CONFIDENT" if extra % 2 == 0 else "TENTATIVE"
                )
        slots[index] = row
    return {"version": VERSION, "seed": seed, "model": model,
            "target": 30, "scenarios": slots}


def validate_plan(plan: dict, settings: HelperSettings, seed: int) -> None:
    if (plan.get("version") != VERSION or plan.get("model") != settings.model
            or plan.get("seed") != seed or plan.get("target") != 30
            or plan.get("helper_settings") != asdict(settings)
            or plan.get("code_fingerprint") != qa.code_fingerprint()):
        raise SystemExit("Plan/settings/helper code changed. Use NEW --output-dir.")
    rows = plan.get("scenarios")
    if not isinstance(rows, list) or len(rows) != 30:
        raise SystemExit("Saved scenarios invalid. Do not execute.")
    if len({r["question_id"] for r in rows}) != 30:
        raise SystemExit("Duplicate question in saved plan. Do not execute.")
    for i, (preferred, ui, outcome) in enumerate(SPECS):
        row = rows[i]
        if (row["id"] != f"S{i+1:03}" or row["speech_language"] != ui
                or row["effective_outcome"] != outcome
                or row["requested_outcome"] != outcome
                or row["presentation_style"] not in FRAMES
                or row["delivery_style"] not in ("CONFIDENT", "TENTATIVE")
                or (i < 9 and code(row["language"]) != code(preferred))
                or (outcome == "UNSURE" and row["selected_key"] is not None)
                or (outcome != "UNSURE"
                    and row["selected_key"] not in row["choices"])):
            raise SystemExit(f"Saved S{i+1:03} invalid. Do not execute.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DIR)
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--max-usd", type=float, default=0.25)
    parser.add_argument("--max-api-requests", type=int, default=60)
    parser.add_argument("--input-usd-per-m", type=float, default=2.5)
    parser.add_argument("--output-usd-per-m", type=float, default=15.0)
    args = parser.parse_args(argv)
    if (args.max_usd <= 0 or args.max_api_requests < 2
            or args.input_usd_per_m < 0 or args.output_usd_per_m < 0):
        parser.error("Invalid budget / price / API-request limit.")
    load_dotenv(qa.BACKEND.parent / ".env", override=False)
    settings = HelperSettings.from_env()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan.json"
    if path.exists():
        plan = json.loads(path.read_text(encoding="utf-8"))
    else:
        plan = build_plan(qa.approved_questions(), args.seed, settings.model)
        plan["helper_settings"] = asdict(settings)
        plan["code_fingerprint"] = qa.code_fingerprint()
        qa.save_json(path, plan)
        print(f"Saved {len(plan['scenarios'])} real approved questions: {path}")
    validate_plan(plan, settings, args.seed)
    rows = plan["scenarios"]
    if not args.execute:
        print("FREE preview; zero model calls.")
        for name, values in (
            ("outcomes", [s["effective_outcome"] for s in rows]),
            ("question languages", [code(s["language"]) for s in rows]),
            ("UI languages", [s["speech_language"] for s in rows]),
            ("frames", [s["presentation_style"] for s in rows]),
            ("advice confidence", [s["delivery_style"] for s in rows
                                   if s["effective_outcome"] != "UNSURE"]),
        ):
            print(name, dict(Counter(values)))
        print("Use --execute to explicitly authorize real paid model calls.")
        return 0
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY missing from repository-root .env")
    sdk = OpenAI()
    done = qa.latest(directory)
    attempts_file = directory / "attempts.jsonl"
    calls_file = directory / "calls.jsonl"
    print(f"Real V6/30: model={settings.model}, 30 consultations, "
          f"up to {args.max_api_requests} attempts, "
          f"estimated guard USD {args.max_usd:.2f}")
    try:
        for row in rows:
            if row["id"] in done:
                continue
            attempts = qa.read_jsonl(attempts_file)
            # Refuse to repeat a possibly charged in-flight call after crash.
            orphan_ids = {a["id"] for a in attempts} - set(done)
            if orphan_ids:
                raise RuntimeError(
                    "Uncommitted API attempts for " + ", ".join(sorted(orphan_ids))
                    + ". No automatic paid replay. Inspect attempts.jsonl."
                )
            spent = sum(
                (a.get("input_tokens", 0)*args.input_usd_per_m +
                 a.get("output_tokens", 0)*args.output_usd_per_m) / 1_000_000
                for a in attempts
            )
            if (spent >= args.max_usd
                    or len(attempts) + settings.max_attempts > args.max_api_requests):
                print("Estimated spend/request guard reached. Stopping.")
                break
            result = qa.evaluate(row, sdk, attempts_file,
                                 args.input_usd_per_m, args.output_usd_per_m)
            qa.append_jsonl(calls_file, result)
            done[row["id"]] = result
            print(f"{row['id']} {row['language']} -> {row['speech_language']} "
                  f"{row['effective_outcome']} {row['presentation_style']}: "
                  f"{result['status']}; calls={result['api_requests']}; "
                  f"fallback={result['is_fallback']}; {result['speech']}")
    except KeyboardInterrupt:
        print("Interrupted. Exporting completed calls.")
    finally:
        archive = qa.export(directory, plan, args.input_usd_per_m,
                            args.output_usd_per_m)
        summary_path = directory / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["qa_goal"] = "V6 30 mixed-language speech, 10/10/10 outcome coverage"
        summary["presentation_styles"] = dict(Counter(
            row["presentation_style"] for row in rows
        ))
        summary["confidence_styles_for_advice"] = dict(Counter(
            row["delivery_style"] for row in rows
            if row["effective_outcome"] != "UNSURE"
        ))
        summary["question_ui_pairs"] = dict(Counter(
            code(row["language"]) + " -> " + row["speech_language"] for row in rows
        ))
        summary["em_dash_speeches"] = sum(
            "\u2014" in item.get("speech", "") for item in done.values()
        )
        qa.save_json(summary_path, summary)
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as archive_zip:
            for name in ("summary.json", "results.csv", "blind_review.csv",
                         "calls.jsonl", "attempts.jsonl"):
                file = directory / name
                if file.exists():
                    archive_zip.write(file, arcname=name)
        print(f"Completed {summary['completed']}/30; "
              f"{summary['api_attempts']} API attempts; "
              f"estimated USD {summary['estimated_spend_usd']:.4f}")
        print(f"Send ZIP: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
