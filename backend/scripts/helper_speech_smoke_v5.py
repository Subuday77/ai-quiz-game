"""Nine-case V5 speech smoke test, with REAL approved PostgreSQL questions.

From backend/ in PyCharm:
    python -m scripts.helper_speech_smoke_v5
    python -m scripts.helper_speech_smoke_v5 --execute

Default = FREE preview. --execute makes at most 18 paid GPT-5.4 calls (two
per case) with a default estimated USD 0.10 budget guard. It tests the
production responder, language/script validator and frozen answer selection.
The database is READ ONLY. Never touches a game, wallet, or consultation.

The nine planned slots deliberately cover:
  German question  -> Russian / English / German UI
  Russian question -> German / English / Russian UI
  English question -> Russian / German / English UI
Each question language includes KNOW, MISTAKEN, and UNSURE. Different question
rows are used for every slot. A MISTAKEN scenario must have a plausible,
visible distractor according to the production selection policy. The planner
fails clearly if the saved pool is not sufficiently populated.

A frozen plan pins responder/settings/selection code and input scenarios.
Run the two commands in the SAME directory; never reuse V1/V2/V3 QA folders.
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

from dotenv import load_dotenv
from openai import OpenAI

from platinum_helper.settings import HelperSettings
from scripts import helper_live_regression as qa

PLAN_VERSION = "platinum-speech-v5-frames-v1"
OUTPUT_DIR = qa.BACKEND / "qa_reports" / "platinum_helper_speech_v5_frames"

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


def make_plan(questions: list[dict], *, seed: int, model: str) -> dict:
    """Use real approved questions and production WRONG-answer plausibility."""
    pool = list(questions)
    random.Random(seed).shuffle(pool)
    used = set()
    slots = []
    for index, (question_language, ui_code, outcome) in enumerate(CASES):
        candidate = None
        for question in pool:
            if (question["question_id"] in used
                    or question["language"].casefold() not in (
                        question_language.casefold(),
                        {"German": "de", "Russian": "ru", "English": "en"}[question_language],
                    )):
                continue
            candidate = qa.scenario(question, outcome, index, seed)
            if candidate is not None:
                break
        if candidate is None:
            raise RuntimeError(
                f"Not enough eligible {question_language} approved questions "
                f"for V5 slot {index+1} ({outcome}). This needs real approved "
                "questions with saved generator plausibility scores. "
                "No scenarios or API calls were fabricated."
            )
        used.add(candidate["question_id"])
        candidate["speech_language"] = ui_code
        # Force balanced coverage ONLY in this QA plan: every frame appears
        # once per question language and once in each KNOW/MISTAKEN/UNSURE
        # outcome. Production picks frame solely from saved call UUID.
        frames = (
            "DIRECT", "PERSONAL", "CASUAL",
            "PERSONAL", "CASUAL", "DIRECT",
            "CASUAL", "DIRECT", "PERSONAL",
        )
        candidate["presentation_style"] = frames[index]
        # Six recommendations: confidence levels are equally represented,
        # with both types among the two samples of each presentation style.
        if outcome != "UNSURE":
            candidate["delivery_style"] = (
                "CONFIDENT" if index in (0, 1, 4) else "TENTATIVE"
            )
        slots.append(candidate)
    return {
        "version": PLAN_VERSION, "seed": seed, "target": CASE_COUNT,
        "model": model, "scenarios": slots,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Opt in to paid real model calls; default is free.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
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
    if saved_plan.exists():
        plan = json.loads(saved_plan.read_text(encoding="utf-8"))
        if (plan.get("version") != PLAN_VERSION
                or plan.get("seed") != args.seed
                or plan.get("model") != settings.model
                or plan.get("target") != CASE_COUNT
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
                "Saved V5 plan/model/settings/code differ; use a NEW --output-dir."
            )
    else:
        plan = make_plan(qa.approved_questions(), seed=args.seed, model=settings.model)
        plan["helper_settings"] = asdict(settings)
        plan["code_fingerprint"] = qa.code_fingerprint()
        qa.save_json(saved_plan, plan)
        print(f"Saved {CASE_COUNT} real-question scenarios "
              f"in {saved_plan}. No API calls yet.")

    if not args.execute:
        print("FREE V5 preview — question / interface / outcome:")
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
    print(f"REAL V5 delivery frames: {settings.model}, {CASE_COUNT} cases, "
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
        summary["qa_goal"] = "V5 independent speaking frame naturalness, strict answer literal, mixed languages"
        summary["question_ui_pairs"] = dict(Counter(
            s["language"] + " -> " + s["speech_language"]
            for s in plan["scenarios"]
        ))
        qa.save_json(directory / "summary.json", summary)
        # The shared exporter has already zipped the summary: update ZIP to
        # include the V5-specific fields without leaving conflicting entries.
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
