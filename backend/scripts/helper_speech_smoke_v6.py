"""Nine-case V6 speech smoke test, reusing verified V5 PostgreSQL questions.

From backend/ in PyCharm:
    python -m scripts.helper_speech_smoke_v6
    python -m scripts.helper_speech_smoke_v6 --execute

Default = FREE preview. --execute makes at most 18 paid GPT-5.4 calls (two
per case) with a default estimated USD 0.10 budget guard. It tests the
production responder, language/script validator and frozen answer selection.
The database is READ ONLY. Never touches a game, wallet, or consultation.

The original V5 nine slots cover:
  German question  -> Russian / English / German UI
  Russian question -> German / English / Russian UI
  English question -> Russian / German / English UI
Each language includes KNOW, MISTAKEN and UNSURE. The original V5 plan
already froze distinct approved question rows and selected plausible, visible
distractors; V6 reuses them verbatim so new answers are directly comparable.

A frozen plan pins responder/settings/selection code and input scenarios.
The nine cases are copied from the ORIGINAL local V5 plan.json for a clean
comparison with unchanged question texts, options, outcome, selected answer,
delivery_style and presentation_style. No fresh question sampling from DB.
Use a NEW V6 output directory and keep the original V5 plan untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from platinum_helper.settings import HelperSettings
from scripts import helper_live_regression as qa

PLAN_VERSION = "platinum-speech-v6-frames-v1"
OUTPUT_DIR = qa.BACKEND / "qa_reports" / "platinum_helper_speech_v6_frames"
BASELINE_PLAN = qa.BACKEND / "qa_reports" / "platinum_helper_speech_v5_frames" / "plan.json"

# (question language, current interface locale, forced gameplay outcome).
# Never inform the responder whether KNOW or MISTAKEN is the correct outcome.
CASES = (
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
CASE_COUNT = len(CASES)


def make_plan_from_v5(previous: dict, *, model: str) -> dict:
    """Freeze identical V5 questions, selected answers AND confidence levels.

    This is a clean within-subject V5 to V6 A/B. V4's original confidence
    levels differed for two scenarios, so don't attribute those differences
    to the new prompt.
    """
    if (previous.get("version") != "platinum-speech-v5-frames-v1"
            or previous.get("model") != model
            or previous.get("target") != CASE_COUNT
            or not isinstance(previous.get("scenarios"), list)
            or len(previous["scenarios"]) != CASE_COUNT):
        raise ValueError("Not a compatible nine-case V5 baseline plan")
    scenarios = deepcopy(previous["scenarios"])
    if len({s["question_id"] for s in scenarios}) != CASE_COUNT:
        raise ValueError("V5 baseline plan reuses questions")
    for idx, (language, ui, outcome) in enumerate(CASES):
        row = scenarios[idx]
        if ((row["language"].casefold() not in {
                  language.casefold(), {"German": "de", "Russian": "ru", "English": "en"}[language]
              })
                or row["speech_language"] != ui
                or row["effective_outcome"] != outcome
                or row["requested_outcome"] != outcome
                or row["presentation_style"] not in ("DIRECT", "PERSONAL", "CASUAL")
                or row["delivery_style"] not in ("CONFIDENT", "TENTATIVE")
                or (outcome != "UNSURE" and row["selected_key"] not in row["choices"])
                or (outcome == "UNSURE" and row["selected_key"] is not None)):
            raise ValueError(f"V5 baseline scenario {idx + 1} is invalid")
    return {
        "version": PLAN_VERSION,
        "seed": previous["seed"],
        "target": CASE_COUNT,
        "model": model,
        "scenarios": scenarios,
    }



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Opt in to paid real model calls; default is free.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--baseline-plan", type=Path, default=BASELINE_PLAN,
                        help="Existing V5 plan.json, preserving questions/answers/confidence")
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--max-usd", type=float, default=0.10)
    parser.add_argument("--max-api-requests", type=int, default=18)
    args = parser.parse_args(argv)
    if args.max_usd <= 0 or args.max_api_requests < 2:
        parser.error("A positive budget and at least 2 allowed API requests are required.")

    load_dotenv(qa.BACKEND.parent / ".env", override=False)
    settings = HelperSettings.from_env()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    saved_plan = directory / "plan.json"
    baseline_path = args.baseline_plan.resolve()
    if not baseline_path.is_file():
        raise SystemExit(
            f"V5 baseline plan not found: {baseline_path}. "
            "Run on the same machine where V5 was tested."
        )
    baseline_bytes = baseline_path.read_bytes()
    baseline_hash = hashlib.sha256(baseline_bytes).hexdigest()
    if saved_plan.exists():
        plan = json.loads(saved_plan.read_text(encoding="utf-8"))
        if (plan.get("version") != PLAN_VERSION
                or plan.get("seed") != args.seed
                or plan.get("model") != settings.model
                or plan.get("target") != CASE_COUNT
                or plan.get("baseline_sha256") != baseline_hash
                or plan.get("helper_settings") != asdict(settings)
                or plan.get("code_fingerprint") != qa.code_fingerprint()
                or [(s["language"].casefold(), s["speech_language"], s["requested_outcome"])
                    for s in plan["scenarios"]]
                   != [(q.casefold(), u, o) for q, u, o in CASES]
                or [s.get("presentation_style") for s in plan["scenarios"]]
                   != ["DIRECT", "PERSONAL", "CASUAL",
                       "PERSONAL", "CASUAL", "DIRECT",
                       "CASUAL", "DIRECT", "PERSONAL"]):
            raise SystemExit(
                "Saved V6 plan/model/settings/code differ; use a NEW --output-dir."
            )
    else:
        previous = json.loads(baseline_bytes.decode("utf-8"))
        if previous.get("helper_settings") != asdict(settings):
            raise SystemExit("V5 helper model/settings have changed. Cannot do controlled V6 comparison.")
        plan = make_plan_from_v5(previous, model=settings.model)
        if plan["seed"] != args.seed:
            raise SystemExit("V5 seed differs. Pass its seed explicitly via --seed.")
        plan["baseline_sha256"] = baseline_hash
        plan["helper_settings"] = asdict(settings)
        plan["code_fingerprint"] = qa.code_fingerprint()
        qa.save_json(saved_plan, plan)
        print(f"Copied {CASE_COUNT} V5 verified scenarios from {baseline_path} "
              f"into {saved_plan}. No DB writes or API calls.")

    if not args.execute:
        print("FREE V6 controlled preview, SAME V5 questions, confidence, frames:")
        for item in plan["scenarios"]:
            print(f"{item['id']}: {item['language']:<8} -> "
                  f"{item['speech_language']:<2}  {item['effective_outcome']:<8} "
                  f"({item['delivery_style']}; {item['presentation_style']})")
        print("Run with --execute ONLY when ready for paid GPT-5.4 speech QA.")
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY missing from the repository-root .env")
    sdk = OpenAI()
    results_path = directory / "calls.jsonl"
    attempts_path = directory / "attempts.jsonl"
    done = qa.latest(directory)
    print(f"REAL V6 delivery frames: {settings.model}, {CASE_COUNT} cases, "
          f"USD estimate cap {args.max_usd}, attempts cap {args.max_api_requests}")
    try:
        for item in plan["scenarios"]:
            if item["id"] in done:
                continue
            attempts = qa.read_jsonl(attempts_path)
            spent = sum(
                (a.get("input_tokens", 0)*2.5 + a.get("output_tokens", 0)*15)
                / 1_000_000 for a in attempts
            )
            if (spent >= args.max_usd
                    or len(attempts) + settings.max_attempts > args.max_api_requests):
                print("Estimated spending/request guard reached. Stopping.")
                break
            result = qa.evaluate(item, sdk, attempts_path, 2.5, 15)
            qa.append_jsonl(results_path, result)
            done[item["id"]] = result
            print(f"{item['id']} {item['language']} -> "
                  f"{item['speech_language']} {item['effective_outcome']}: "
                  f"{result['status']}; requests={result['api_requests']}; "
                  f"fallback={result['is_fallback']}; {result['speech']}")
    except KeyboardInterrupt:
        print("Interrupted; saved partial results will be exported.")
    finally:
        archive = qa.export(directory, plan, 2.5, 15)
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        summary["qa_goal"] = "V6 strict no-em-dash speech and distinct presentation vs identical V5 scenarios"
        summary["question_ui_pairs"] = dict(Counter(
            s["language"] + " -> " + s["speech_language"]
            for s in plan["scenarios"]
        ))
        qa.save_json(directory / "summary.json", summary)
        # The shared exporter has already zipped the summary: update ZIP to
        # include the V6-specific fields without leaving conflicting entries.
        from zipfile import ZipFile, ZIP_DEFLATED
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipfile:
            for name in ("summary.json", "results.csv", "blind_review.csv",
                         "calls.jsonl", "attempts.jsonl"):
                path = directory / name
                if path.exists():
                    zipfile.write(path, arcname=name)
        print(f"Completed {summary['completed']}/{CASE_COUNT}; "
              f"{summary['api_attempts']} API attempts; "
              f"estimated USD {summary['estimated_spend_usd']:.4f}")
        print(f"Send this ZIP: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
