"""Controlled CASUAL speech recheck after 30-case V6 review.

This replays exactly S007 (Russian UI/TENTATIVE), S016 (Japanese UI/
CONFIDENT) and S023 (Arabic UI/TENTATIVE) from the existing LOCAL V6/30
plan, with the new CASUAL prompt. Never changes a previous QA result.

Run in PyCharm, from backend/:
  python -m scripts.helper_casual_smoke_v6           # free preview
  python -m scripts.helper_casual_smoke_v6 --execute # 3 GPT-5.4 consultations

Existing V6/30 plan.json is required. DB is not modified or queried.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import load_dotenv
from openai import OpenAI

from platinum_helper.settings import HelperSettings
from scripts import helper_live_regression as qa

VERSION = "platinum-speech-v6-casual-fix-v1"
SOURCE = qa.BACKEND / "qa_reports" / "platinum_helper_speech_v6_30" / "plan.json"
OUTPUT = qa.BACKEND / "qa_reports" / "platinum_helper_speech_v6_casual_fix"
TARGETS = (
    ("S007", "ru", "KNOW", "TENTATIVE"),
    ("S016", "ja", "KNOW", "CONFIDENT"),
    ("S023", "ar", "MISTAKEN", "TENTATIVE"),
)


def sample_previous(previous: dict, *, model: str) -> list[dict]:
    """Select exact old troublesome samples; never resample or repick answers."""
    if (previous.get("version") != "platinum-speech-v6-multilingual-30-v1"
            or previous.get("target") != 30 or previous.get("model") != model
            or not isinstance(previous.get("scenarios"), list)
            or len(previous["scenarios"]) != 30):
        raise ValueError("Not an authentic V6 30-case baseline plan")
    catalog = {row["id"]: row for row in previous["scenarios"]}
    if len(catalog) != 30:
        raise ValueError("V6 baseline repeats scenario IDs")
    chosen = []
    for ident, lang, outcome, confidence in TARGETS:
        row = catalog[ident]
        if (row.get("speech_language") != lang
                or row.get("effective_outcome") != outcome
                or row.get("requested_outcome") != outcome
                or row.get("delivery_style") != confidence
                or row.get("presentation_style") != "CASUAL"
                or row.get("selected_key") not in row.get("choices", {})):
            raise ValueError("Unexpected baseline content for " + ident)
        chosen.append(deepcopy(row))
    if len({row["question_id"] for row in chosen}) != len(TARGETS):
        raise ValueError("Duplicate question ID in CASUAL baseline")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--baseline-plan", type=Path, default=SOURCE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--max-usd", type=float, default=0.04)
    parser.add_argument("--max-api-requests", type=int, default=6)
    args = parser.parse_args(argv)
    if args.max_usd <= 0 or args.max_api_requests < 2:
        parser.error("Budget must be positive and request cap >= 2.")

    load_dotenv(qa.BACKEND.parent / ".env", override=False)
    settings = HelperSettings.from_env()
    source = args.baseline_plan.resolve()
    if not source.is_file():
        raise SystemExit("Missing original V6/30 plan.json: " + str(source))
    raw = source.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    try:
        previous = json.loads(raw.decode("utf-8"))
        cases = sample_previous(previous, model=settings.model)
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit("Invalid V6/30 baseline: " + str(exc)) from exc

    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan.json"
    if path.exists():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if (plan.get("version") != VERSION
                or plan.get("source_sha256") != source_sha
                or plan.get("code_fingerprint") != qa.code_fingerprint()
                or plan.get("helper_settings") != asdict(settings)
                or plan.get("scenarios") != cases):
            raise SystemExit(
                "Frozen QA plan, helper code or baseline changed. "
                "Use NEW --output-dir; do not mix paid versions."
            )
    else:
        plan = {
            "version": VERSION, "target": 3, "model": settings.model,
            "scenarios": cases, "source_sha256": source_sha,
            "code_fingerprint": qa.code_fingerprint(),
            "helper_settings": asdict(settings),
        }
        qa.save_json(path, plan)
        print("Frozen identical S007/S016/S023 from the earlier V6/30 plan.")

    if not args.execute:
        print("FREE preview: 3 targeted CASUAL consultations, zero API calls.")
        for case in plan["scenarios"]:
            print(case["id"], case["language"], "->", case["speech_language"],
                  case["effective_outcome"], case["delivery_style"],
                  case["choices"][case["selected_key"]])
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Missing OPENAI_API_KEY in repository-root .env")
    done = qa.latest(directory)
    attempts_path = directory / "attempts.jsonl"
    sdk = OpenAI()
    try:
        for case in plan["scenarios"]:
            if case["id"] in done:
                continue
            attempts = qa.read_jsonl(attempts_path)
            interrupted = {a["id"] for a in attempts} - set(done)
            if interrupted:
                raise RuntimeError(
                    "Recorded API attempts but missing completed result for "
                    + ", ".join(sorted(interrupted))
                    + ". Do not repeat a possibly charged call automatically."
                )
            estimated = sum(
                (a.get("input_tokens", 0) * 2.5
                 + a.get("output_tokens", 0) * 15.0) / 1_000_000
                for a in attempts
            )
            if (estimated >= args.max_usd
                    or len(attempts) + settings.max_attempts > args.max_api_requests):
                print("Estimated budget/request cap reached; stopping.")
                break
            result = qa.evaluate(case, sdk, attempts_path, 2.5, 15)
            qa.append_jsonl(directory / "calls.jsonl", result)
            done[case["id"]] = result
            print(case["id"], result["status"], "calls=", result["api_requests"],
                  "fallback=", result["is_fallback"], result["speech"])
    except KeyboardInterrupt:
        print("Interrupted; exporting partial results.")
    finally:
        archive = qa.export(directory, plan, 2.5, 15)
        summary = json.loads((directory / "summary.json").read_text("utf-8"))
        summary["qa_goal"] = "Recheck V6 CASUAL ru/ar/ja exact previous scenarios"
        summary["previous_ids"] = [row[0] for row in TARGETS]
        qa.save_json(directory / "summary.json", summary)
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
            for name in ("summary.json", "results.csv", "blind_review.csv",
                         "calls.jsonl", "attempts.jsonl"):
                item = directory / name
                if item.is_file():
                    output.write(item, arcname=name)
        print("Completed", summary["completed"], "/3; estimated USD",
              summary["estimated_spend_usd"], "; ZIP:", archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
