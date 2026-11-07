"""Read-only, resumable live QA of the production Platinum Helper.

From the backend directory in PyCharm (DATABASE_URL and OPENAI_API_KEY configured):
  python -m scripts.helper_live_regression --limit 30
  python -m scripts.helper_live_regression --execute --limit 30
  python -m scripts.helper_live_regression --execute --limit 150

Without --execute this is a free preview. The same 150-case plan is saved
on first invocation; resuming never repeats completed cases. The script reads
approved PostgreSQL questions but NEVER creates games, debits stars, changes
questions or invokes the player-facing purchase/call endpoints.

Outcomes are FORCED to test guardrails, not sampled from competence weights.
The actual responder.talk(), its 8-second timeout, prompts, retries, validator
and fallback are used unchanged. These tests do not replace browser QA.
"""
from __future__ import annotations

import argparse
import hashlib
import csv
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict
from uuid import UUID, NAMESPACE_URL, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from persistence.database import engine
from persistence.models import Question, QuestionReview, Topic
from platinum_helper.core import CATEGORY_TOPICS, PROFESSIONAL_SKILLS, choose_delivery_style, choose_presentation_style, generate_profile
from platinum_helper.responder import HelperUnavailable, _DENIAL, _REASK, _mentions, _validate, talk
from platinum_helper.selection import best_visible_wrong
from platinum_helper.settings import HelperSettings
from star_perks import PerkUnavailable, choose_to_hide

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BACKEND / "qa_reports" / "platinum_helper_live"
PLAN_VERSION = "platinum-live-v1"
MODE_CYCLE = ("KNOW", "MISTAKEN", "MISTAKEN", "KNOW", "UNSURE",
              "MISTAKEN", "KNOW", "ABSTAIN", "MISTAKEN", "UNSURE")
LANGUAGES = {
    "russian", "english", "german", "french", "spanish", "italian",
    "portuguese", "hebrew", "arabic", "japanese", "chinese",
    "ru", "en", "de", "fr", "es", "it", "pt", "he", "ar", "ja", "zh",
}
REVIEW_FIELDS = (
    "review_id", "question", "options", "speech", "naturalness_1_5",
    "invented_fact_0_1", "implicit_answer_leak_0_1",
    "self_contradiction_0_1", "style_natural_1_5", "notes",
)


def code_fingerprint() -> str:
    """Pin responder/selection/settings revisions for reproducible paid QA."""
    digest = hashlib.sha256()
    for name in ("responder.py", "selection.py", "settings.py", "core.py"):
        path = BACKEND / "platinum_helper" / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Broken JSONL {path.name}, line {index}: {exc}") from exc
    return rows


def approved_questions() -> list[dict]:
    """Read original scores with the same first-review policy as production."""
    with Session(engine(), autoflush=False) as db:
        try:
            db.execute(text("SET TRANSACTION READ ONLY"))
            reviews = {}
            for review in db.scalars(
                select(QuestionReview).where(
                    QuestionReview.checker_type == "generator_plausibility"
                ).order_by(QuestionReview.created_at, QuestionReview.id)
            ):
                ident = str(review.question_id)
                if ident not in reviews:
                    details = review.details if isinstance(review.details, dict) else {}
                    reviews[ident] = details.get("scores")
            result = []
            for question, topic in db.execute(
                select(Question, Topic).join(Topic, Question.topic_id == Topic.id)
                .where(Question.status == "approved").order_by(Question.id)
            ):
                labels = topic.labels if isinstance(topic.labels, dict) else {}
                broad = CATEGORY_TOPICS.get(labels.get("en", ""))
                choices = question.choices
                if (not broad or not isinstance(choices, dict)
                        or set(choices) != set("ABCD")
                        or any(not isinstance(v, str) or not v.strip() for v in choices.values())
                        or question.correct_key not in "ABCD"
                        or question.language.strip().casefold() not in LANGUAGES):
                    continue
                result.append({
                    "question_id": str(question.id), "language": question.language,
                    "difficulty": int(question.requested_difficulty),
                    "category": labels.get("en", ""), "topic": broad,
                    "question": question.text, "choices": dict(choices),
                    "correct_key": question.correct_key,
                    "scores": reviews.get(str(question.id)),
                })
            return result
        finally:
            db.rollback()


def scenario(question: dict, mode: str, index: int, seed: int) -> dict | None:
    """Exercise actual bronze + plausible mistake policies, without DB writes."""
    local_seed = seed + 997 * index + int(UUID(question["question_id"]).int % 100000)
    bronze = index % 3
    try:
        hidden = (list(choose_to_hide(
            question["correct_key"], question["scores"], count=bronze,
            rng=random.Random(local_seed),
        )) if bronze else [])
        wrong = best_visible_wrong(
            correct_key=question["correct_key"], difficulty=question["difficulty"],
            scores=question["scores"], hidden=hidden, rng=random.Random(local_seed),
        )
    except (PerkUnavailable, TypeError, ValueError):
        return None
    if mode == "MISTAKEN" and wrong is None:
        return None
    if mode == "ABSTAIN" and wrong is not None:
        return None
    outcome = "UNSURE" if mode in ("ABSTAIN", "UNSURE") else mode
    selected = question["correct_key"] if outcome == "KNOW" else (
        wrong if outcome == "MISTAKEN" else None
    )
    profession = sorted(PROFESSIONAL_SKILLS)[index % len(PROFESSIONAL_SKILLS)]
    is_ru = question["language"].casefold() in ("ru", "russian")
    profile = generate_profile(
        profession, name="Алексей" if is_ru else "Alex", gender="male",
        rng=random.Random(local_seed),
    )
    return {
        "id": f"S{index+1:03}", **question,
        "hidden_keys": hidden, "bronze_removed": bronze,
        "profession": profession, "name": profile.name,
        "gender": profile.gender, "nationality": profile.nationality,
        "competence": profile.skills[question["topic"]],
        "requested_outcome": "MISTAKEN" if mode == "ABSTAIN" else mode,
        "effective_outcome": outcome, "selected_key": selected,
        "delivery_style": choose_delivery_style(
            uuid5(NAMESPACE_URL, f"platinum-qa:{seed}:{index}")
        ),
        "presentation_style": choose_presentation_style(
            uuid5(NAMESPACE_URL, f"platinum-qa:{seed}:{index}")
        ),
    }


def build_plan(questions: list[dict], target: int, seed: int, model: str) -> dict:
    if not 1 <= target <= 1000:
        raise ValueError("--target must be 1..1000")
    pool = list(questions)
    random.Random(seed).shuffle(pool)
    available = {q["question_id"] for q in pool}
    modes = [MODE_CYCLE[i % len(MODE_CYCLE)] for i in range(target)]
    slots: list[dict | None] = [None] * target
    # Constrained mistakes and abstentions claim their questions first.
    for mode in ("MISTAKEN", "ABSTAIN", "KNOW", "UNSURE"):
        for index, wanted in enumerate(modes):
            if wanted != mode:
                continue
            for q in pool:
                if q["question_id"] not in available:
                    continue
                candidate = scenario(q, mode, index, seed)
                if candidate is not None:
                    slots[index] = candidate
                    available.remove(q["question_id"])
                    break
            else:
                raise RuntimeError(
                    f"Insufficient eligible questions for {mode}. "
                    "Reduce --target or seed more approved questions."
                )
    return {
        "version": PLAN_VERSION, "seed": seed, "target": target,
        "model": model, "created_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": slots,
    }


def frozen_plan(directory: Path, args: argparse.Namespace) -> dict:
    path = directory / "plan.json"
    if path.exists():
        plan = json.loads(path.read_text(encoding="utf-8"))
        existing = (plan.get("version"), plan.get("seed"), plan.get("target"), plan.get("model"))
        requested = (PLAN_VERSION, args.seed, args.target, args.model)
        if existing != requested:
            raise RuntimeError("Plan already exists for another seed/target/model. "
                               "Reuse original options or change --output-dir.")
        return plan
    items = approved_questions()
    if not items:
        raise RuntimeError("No approved PostgreSQL questions found. Check DATABASE_URL.")
    plan = build_plan(items, args.target, args.seed, args.model)
    save_json(path, plan)
    print(f"Frozen {args.target} cases from {len(items)} approved questions.")
    return plan


def responder_data(s: dict) -> dict:
    return {
        "name": s["name"], "profession": s["profession"],
        "gender": s["gender"], "nationality": s["nationality"],
        "language": s["language"],
        **({"speech_language": s["speech_language"]} if "speech_language" in s else {}),
        "outcome": s["effective_outcome"],
        "letter": s["selected_key"], "question_text": s["question"],
        "choices": s["choices"],
        "correct_option_text": s["choices"][s["correct_key"]],
        "topic": s["topic"], "delivery_style": s["delivery_style"],
        "presentation_style": s.get("presentation_style", "DIRECT"),
    }


class TraceClient:
    """Record every paid Responses API attempt, including invalid drafts."""
    def __init__(self, sdk, ident: str, attempts_path: Path):
        self.sdk = sdk
        self.ident = ident
        self.path = attempts_path
        self.events: list[dict] = []

    def with_options(self, **kwargs):
        self.sdk = self.sdk.with_options(**kwargs)
        return self

    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        t0 = time.perf_counter()
        event = {
            "id": self.ident, "attempt": len(self.events)+1,
            "model": kwargs.get("model"),
            "strict_prompt": "earlier response did not meet" in kwargs.get(
                "instructions", ""
            ).lower(),
        }
        try:
            response = self.sdk.responses.create(**kwargs)
            usage = getattr(response, "usage", None)
            event.update({
                "status": getattr(response, "status", None),
                "raw_text": getattr(response, "output_text", "") or "",
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "error": "",
            })
            return response
        except Exception as exc:
            event.update({
                "status": "exception", "raw_text": "",
                "input_tokens": 0, "output_tokens": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            })
            raise
        finally:
            event["seconds"] = round(time.perf_counter()-t0, 3)
            self.events.append(event)
            append_jsonl(self.path, event)


def evaluate(s: dict, sdk: OpenAI, attempts_path: Path,
             input_price: float, output_price: float) -> dict:
    data = responder_data(s)
    trace = TraceClient(sdk, s["id"], attempts_path)
    t0 = time.perf_counter()
    status, speech, error = "completed", "", ""
    try:
        speech = talk(data, client=trace)
    except HelperUnavailable as exc:
        status, error = "unavailable", str(exc)
    except Exception as exc:
        status, error = "error", f"{type(exc).__name__}: {str(exc)[:180]}"
    valid_drafts = [
        bool(e.get("raw_text") and _validate(e["raw_text"], data) is not None)
        for e in trace.events
    ]
    fallback = bool(speech) and not any(valid_drafts)
    rejections = sum(
        e.get("status") == "completed" and not valid
        for e, valid in zip(trace.events, valid_drafts)
    )
    if speech:
        final_raw = (
            "SPEECH: " + speech if s["effective_outcome"] == "UNSURE"
            else f"KEY: {s['selected_key']}\nSPEECH: {speech}"
        )
        if _validate(final_raw, data) is None:
            status, error = "unsafe_final", "Final speech failed current validator"
    tokens_in = sum(e.get("input_tokens", 0) for e in trace.events)
    tokens_out = sum(e.get("output_tokens", 0) for e in trace.events)
    return {
        "id": s["id"], "status": status, "speech": speech, "error": error,
        "is_fallback": fallback, "guard_rejections": rejections,
        "api_requests": len(trace.events),
        "input_tokens": tokens_in, "output_tokens": tokens_out,
        "seconds": round(time.perf_counter()-t0, 3),
        "estimated_usd": round(
            (tokens_in*input_price + tokens_out*output_price)/1_000_000, 7
        ),
        "literal_correct_leak": bool(speech) and s["effective_outcome"] == "MISTAKEN"
                                and _mentions(speech, data["correct_option_text"]),
        "self_denial": bool(_DENIAL.search(speech)) if speech else False,
        "reask": bool(_REASK.search(speech)) if speech else False,
    }


def latest(directory: Path) -> dict[str, dict]:
    return {r["id"]: r for r in read_jsonl(directory / "calls.jsonl")}


def export(directory: Path, plan: dict, input_price: float, output_price: float) -> Path:
    results = latest(directory)
    attempts = read_jsonl(directory / "attempts.jsonl")
    table, review = [], []
    for s in plan["scenarios"]:
        result = results.get(s["id"], {})
        row = {
            "id": s["id"], "question_id": s["question_id"],
            "language": s["language"],
            "ui_language": s.get("speech_language", s["language"]),
            "difficulty": s["difficulty"],
            "category": s["category"], "topic": s["topic"],
            "question": s["question"],
            **{f"choice_{k.lower()}": s["choices"][k] for k in "ABCD"},
            "correct_key": s["correct_key"],
            "correct_answer": s["choices"][s["correct_key"]],
            "scores": json.dumps(s["scores"], ensure_ascii=False),
            "hidden_keys": ",".join(s["hidden_keys"]),
            "profession": s["profession"], "competence": s["competence"],
            "requested_outcome": s["requested_outcome"],
            "effective_outcome": s["effective_outcome"],
            "selected_key": s["selected_key"] or "",
            "selected_answer": s["choices"][s["selected_key"]] if s["selected_key"] else "",
            "delivery_style": s["delivery_style"],
            "presentation_style": s.get("presentation_style", "DIRECT"),
            **result,
        }
        table.append(row)
        if result:
            review.append({
                "review_id": s["id"], "question": s["question"],
                "ui_language": s.get("speech_language", s["language"]),
                "question_language": s["language"],
                "options": " | ".join(f"{k}: {s['choices'][k]}" for k in "ABCD"),
                "speech": result.get("speech", ""),
                "naturalness_1_5": "", "invented_fact_0_1": "",
                "implicit_answer_leak_0_1": "", "self_contradiction_0_1": "",
                "style_natural_1_5": "", "notes": "",
            })
    for filename, rows in (("results.csv", table), ("blind_review.csv", review)):
        fields = list(dict.fromkeys(key for row in rows for key in row)) if rows else (REVIEW_FIELDS if filename == "blind_review.csv" else ["id"])
        with (directory / filename).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    spent = sum(
        (a.get("input_tokens", 0)*input_price + a.get("output_tokens", 0)*output_price)
        / 1_000_000 for a in attempts
    )
    summary = {
        "version": plan["version"], "model": plan["model"], "target": plan["target"],
        "helper_settings": plan.get("helper_settings"),
        "code_fingerprint": plan.get("code_fingerprint"),
        "completed": len(results), "remaining": plan["target"] - len(results),
        "statuses": dict(Counter(r.get("status") for r in results.values())),
        "planned_effective_outcomes": dict(Counter(s["effective_outcome"] for s in plan["scenarios"])),
        "languages": dict(Counter(s["language"] for s in plan["scenarios"])),
        "ui_languages": dict(Counter(s.get("speech_language", s["language"])
                                      for s in plan["scenarios"])),
        "api_attempts": len(attempts),
        "guard_rejections": sum(r.get("guard_rejections", 0) for r in results.values()),
        "fallbacks": sum(bool(r.get("is_fallback")) for r in results.values()),
        "literal_correct_leaks": sum(bool(r.get("literal_correct_leak")) for r in results.values()),
        "estimated_spend_usd": round(spent, 5),
        "assumed_price_usd_per_million": {"input": input_price, "output": output_price},
        "limitations": [
            "Forced outcomes test response policy, not actual competence probability distribution.",
            "Model token prices are estimates, not official billing.",
            "Automated checks do not catch all semantic leaks; manually inspect blind_review.csv.",
            "The database is read-only; no game purchases or hints were charged.",
        ],
    }
    save_json(directory / "summary.json", summary)
    dest = directory / "platinum_helper_live_results.zip"
    with ZipFile(dest, "w", compression=ZIP_DEFLATED) as archive:
        for name in ("summary.json", "results.csv", "blind_review.csv",
                     "calls.jsonl", "attempts.jsonl"):
            path = directory / name
            if path.exists():
                archive.write(path, arcname=name)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Make real paid API calls; without this flag ONLY preview")
    parser.add_argument("--limit", type=int, default=30,
                        help="Cumulative target for this run; 30 first, then 150")
    parser.add_argument("--target", type=int, default=150)
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--model", default=None,
                        help="Explicit override; otherwise read QUIZ_HELPER_MODEL_ID from the root .env")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-usd", type=float, default=3.0)
    parser.add_argument("--max-api-requests", type=int, default=350)
    parser.add_argument("--input-usd-per-m", type=float, default=2.5)
    parser.add_argument("--output-usd-per-m", type=float, default=15.0)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if (not 1 <= args.limit <= args.target or args.max_usd <= 0
            or args.max_api_requests < 2 or args.input_usd_per_m < 0
            or args.output_usd_per_m < 0):
        parser.error("Invalid limit, target or budget")
    load_dotenv(BACKEND.parent / ".env", override=False)
    if args.model is not None:
        # Only an explicit CLI argument may override the shared environment.
        os.environ["QUIZ_HELPER_MODEL_ID"] = args.model
    helper_settings = HelperSettings.from_env()
    args.model = helper_settings.model
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    plan_path = directory / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (plan.get("version"), plan.get("seed"), plan.get("target"), plan.get("model")) != (
                PLAN_VERSION, args.seed, args.target, args.model):
            raise SystemExit("Plan settings changed. Use a new --output-dir.")
        if plan.get("code_fingerprint") != code_fingerprint():
            raise SystemExit("Platinum Helper code changed since this QA plan was saved. "
                             "Use a NEW --output-dir; never mix versions in one run.")
        if (plan.get("helper_settings") is not None
                and plan["helper_settings"] != asdict(helper_settings)):
            raise SystemExit("Platinum Helper settings changed since the plan was saved. "
                             "Use a new --output-dir for a comparable run.")
    else:
        questions = approved_questions()
        plan = build_plan(questions, args.target, args.seed, args.model)
        plan["helper_settings"] = asdict(helper_settings)
        plan["code_fingerprint"] = code_fingerprint()
        save_json(plan_path, plan)
        print(f"Saved {args.target} cases from {len(questions)} approved questions.")
    if not args.execute:
        print(f"PREVIEW: {args.limit} of {args.target} cases, ZERO paid API calls.")
        print("Outcomes:", dict(Counter(s["effective_outcome"] for s in plan["scenarios"][:args.limit])))
        print("Languages:", dict(Counter(s["language"] for s in plan["scenarios"][:args.limit])))
        return 0
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY missing (configure the repository-root .env)")
    sdk = OpenAI()
    results_file = directory / "calls.jsonl"
    attempts_file = directory / "attempts.jsonl"
    done = latest(directory)
    print(f"REAL API: {args.model}; reasoning={helper_settings.reasoning_effort}, "
          f"max_tokens={helper_settings.max_output_tokens}, "
          f"timeout={helper_settings.timeout_seconds}s, "
          f"max_attempts={helper_settings.max_attempts}, "
          f"plausibility={helper_settings.threshold_level_0}/"
          f"{helper_settings.threshold_levels_1_3}/"
          f"{helper_settings.threshold_levels_4_15}; "
          f"requested {args.limit}/{args.target} scenarios.")
    try:
        for s in plan["scenarios"][:args.limit]:
            previous = done.get(s["id"])
            if previous and not (args.retry_failed and previous.get("status") != "completed"):
                continue
            attempts = read_jsonl(attempts_file)
            spent = sum(
                (e.get("input_tokens", 0)*args.input_usd_per_m +
                 e.get("output_tokens", 0)*args.output_usd_per_m)/1_000_000
                for e in attempts
            )
            if (spent >= args.max_usd or
                    len(attempts) + helper_settings.max_attempts > args.max_api_requests):
                print("Budget/API request limit reached; stop before new scenario.")
                break
            row = evaluate(s, sdk, attempts_file, args.input_usd_per_m,
                           args.output_usd_per_m)
            append_jsonl(results_file, row)
            done[s["id"]] = row
            print(f"{s['id']} {s['effective_outcome']:8s} "
                  f"{row['status']:13s} calls={row['api_requests']} "
                  f"fallback={row['is_fallback']} USD {row['estimated_usd']:.5f}")
    except KeyboardInterrupt:
        print("\nInterrupted; saved results will be exported.")
    finally:
        archive = export(directory, plan, args.input_usd_per_m, args.output_usd_per_m)
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        print(f"Saved {summary['completed']}/{args.target}; "
              f"{summary['api_attempts']} API calls; "
              f"estimated USD {summary['estimated_spend_usd']:.4f}")
        print(f"Send this ZIP: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
