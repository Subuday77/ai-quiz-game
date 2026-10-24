"""Live calibration for independent factual answer validation (not in game).

Run from project root, with OPENAI_API_KEY in project .env:
    python test_openai_answer_check.py
    python test_openai_answer_check.py --limit 4

The model NEVER sees the supplied key or provisional human annotations.
Requires two distinct checks: the strict local answer-leak filter and this
independent answer validity test. No auto-rejection based on model output.
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
load_dotenv(ROOT / ".env")

from independent_answer_check import (
    PROMPT, SCHEMA_NAME, IndependentAnswerReview, answer_review_schema,
    blind_payload, summarize_answer_review,
)
from local_leak_filter import exact_answer_leak
from question_schema import QuizQuestion


MODEL = os.getenv("AI_QUALITY_MODEL_ID", "gpt-5.4-mini")
REASONING = {"effort": "low"}

# These are PROVISIONAL human editorial labels, NOT fine-tuning labels.
# The 2026-10-24 A/B incidents motivated the high-level regression examples.
# Confirm obscure historical sources before using any case as gold training data.
# Intentional wrong-key examples test whether the reviewer resists key anchoring.
CASES = (
    dict(id="piano", level=0, stem="На каком инструменте обычно играют с помощью клавиш?",
         answers=("Скрипка", "Пианино", "Труба", "Барабан"), key="B",
         expected="single_correct", accepted="B"),
    dict(id="brazil", level=1, stem="Какой язык является официальным языком Бразилии?",
         answers=("Испанский", "Португальский", "Итальянский", "Французский"),
         key="B", expected="single_correct", accepted="B"),
    dict(id="saturn", level=5, stem="Какая планета известна своими хорошо заметными кольцами?",
         answers=("Земля", "Марс", "Сатурн", "Венера"),
         key="C", expected="single_correct", accepted="C"),
    dict(id="saturn_wrong_key", level=5,
         stem="Какая планета известна своими хорошо заметными кольцами?",
         answers=("Венера", "Марс", "Сатурн", "Земля"),
         key="A", expected="single_correct", accepted="C"),
    dict(id="slogan", level=6,
         stem="С каким сериалом связан слоган «The truth is out there»?",
         answers=("Твин Пикс", "Грань", "Секретные материалы", "Остаться в живых"),
         key="C", expected="single_correct", accepted="C"),
    dict(id="hanami", level=7,
         stem="Как называется японская традиция любования цветением сакуры?",
         answers=("Танабата", "Ханами", "Сэцубун", "Обон"),
         key="B", expected="single_correct", accepted="B"),
    dict(id="enclave_three_correct", level=10,
         stem="Какое из этих государств не имеет выхода к морю, но полностью окружено территорией только одной страны?",
         answers=("Лесото", "Парагвай", "Сан-Марино", "Ватикан"),
         key="A", expected="multiple_correct", accepted="ACD"),
    dict(id="mrs_dalloway_direct_leak", level=11,
         stem="Какой роман Вирджинии Вулф начинается словами о том, что миссис Дэллоуэй сама купит цветы?",
         answers=("На маяк", "Орландо", "Волны", "Миссис Дэллоуэй"),
         key="D", expected="single_correct", accepted="D"),
    dict(id="mussorgsky_versions", level=12,
         stem="Какой композитор завершил и оркестровал оперу Модеста Мусоргского «Хованщина» в редакции, долгое время шедшей на сцене в XX веке?",
         answers=("Николай Римский-Корсаков", "Александр Глазунов",
                  "Дмитрий Шостакович", "Сергей Танеев"),
         key="A", expected="uncertain", accepted=""),
    dict(id="dirac", level=13,
         stem="Какой физик в 1928 году теоретически предсказал позитрон из релятивистского волнового уравнения?",
         answers=("Паули", "Шрёдингер", "Дирак", "Гейзенберг"),
         key="C", expected="single_correct", accepted="C"),
    dict(id="hanno_periplus", level=14,
         stem="Какой античный автор является главным источником сведений о путешествии Ганнона, чей текст дошёл под названием «Перипл Ганнона»?",
         answers=("Плутарх", "Арриан", "Псевдо-Скилак", "Геродот"),
         key="C", expected="no_correct", accepted=""),
    dict(id="ebla_direct_leak", level=14,
         stem="Какой древний город, столица царства Эбла, идентифицировали как Телль-Мардих?",
         answers=("Мари", "Угарит", "Эбла", "Ниневия"),
         key="C", expected="single_correct", accepted="C"),
    dict(id="empty_answer_set", level=2,
         stem="Какой из этих городов является столицей Франции?",
         answers=("Рим", "Осло", "Берлин", "Берн"),
         key="A", expected="no_correct", accepted=""),
    dict(id="incorrect_nickname_premise", level=5,
         stem="Какой русский князь носил историческое прозвище «Длиннорукий»?",
         answers=("Юрий Долгорукий", "Александр Невский",
                  "Владимир Мономах", "Иван Калита"),
         key="A", expected="invalid_premise", accepted=""),
)


def payload_for_case(case: dict) -> tuple[QuizQuestion, str]:
    question = QuizQuestion.model_validate({
        "difficulty": case["level"], "question": case["stem"],
        "answers": dict(zip("ABCD", case["answers"])),
        "correct_answer": case["key"],
    })
    return question, blind_payload(question, "Russian")


def run_case(client: OpenAI, case: dict) -> dict:
    q, payload = payload_for_case(case)
    start = time.perf_counter()
    response = client.responses.create(
        model=MODEL,
        instructions=PROMPT,
        input=payload,
        max_output_tokens=1500,
        temperature=1,
        reasoning=REASONING,
        text={"format": {"type": "json_schema", "name": SCHEMA_NAME,
                         "strict": True, "schema": answer_review_schema()}},
    )
    if response.status != "completed" or not response.output_text:
        raise RuntimeError("Independent verifier output incomplete")
    review = IndependentAnswerReview.model_validate_json(response.output_text)
    summary = summarize_answer_review(review)
    predicted = "".join(summary["supported_choices"])
    return {
        "case": case["id"], "level": case["level"],
        "expected": case["expected"], "observed": summary["outcome"],
        "status_match": summary["outcome"] == case["expected"],
        "expected_choices": case["accepted"], "observed_choices": predicted,
        "choices_match": predicted == case["accepted"] if case["accepted"] else None,
        "generator_key": q.correct_answer, "key_supported": q.correct_answer in predicted,
        "local_leak": exact_answer_leak(q),
        "per_option": {k: v.model_dump(mode="json") for k, v in review.options.items()},
        "premise": review.premise.value, "rationale": review.rationale,
        "duration_seconds": round(time.perf_counter() - start, 3),
        "input_tokens": response.usage.input_tokens if response.usage else None,
        "output_tokens": response.usage.output_tokens if response.usage else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=len(CASES),
                        help="First N frozen cases (default: all)")
    args = parser.parse_args()
    if not 1 <= args.limit <= len(CASES):
        parser.error(f"--limit must be 1..{len(CASES)}")
    key = os.getenv("OPENAI_API_KEY")
    if not key or key.startswith("REPLACE_"):
        parser.error("Set OPENAI_API_KEY in the root .env before running")
    client = OpenAI(api_key=key, timeout=30, max_retries=0)
    prefix = ROOT / f"answer_check_{datetime.now():%Y%m%d_%H%M%S}"
    rows = []
    print(f"KEY-BLIND ANSWER CHECK | {MODEL} | {args.limit} cases")
    for i, case in enumerate(CASES[:args.limit], 1):
        try:
            row = run_case(client, case)
        except Exception as exc:
            row = {"case": case["id"], "expected": case["expected"],
                   "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        rows.append(row)
        print(f"[{i}/{args.limit}] {case['id']} | expected={case['expected']} "
              f"| observed={row.get('observed', 'ERROR')} "
              f"| key={row.get('generator_key', '?')} "
              f"| choices={row.get('observed_choices', '?')}")
        if "error" in row:
            print(f"    ERROR {row['error']}")
    with prefix.with_suffix(".jsonl").open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    fields = ["case", "level", "expected", "observed", "status_match",
              "expected_choices", "observed_choices", "choices_match",
              "generator_key", "key_supported", "local_leak", "premise",
              "rationale", "per_option", "duration_seconds", "input_tokens",
              "output_tokens", "error"]
    with prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as out:
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    valid = [r for r in rows if "error" not in r]
    scored = [r for r in valid if r["expected"] != "uncertain"]
    print(f"COMPLETE: {len(valid)}/{len(rows)}; known-class status matches: "
          f"{sum(r['status_match'] for r in scored)}/{len(scored)}")
    good = [r for r in scored if r["expected"] == "single_correct"]
    flawed = [r for r in scored if r["expected"] != "single_correct"]
    print(f"SAFE SINGLE-ANSWER ACCURACY: {sum(r['status_match'] for r in good)}/{len(good)}")
    print(f"INVALID-ANSWER DETECTION: {sum(r['observed'] != 'single_correct' for r in flawed)}/{len(flawed)}")
    print(f"JSONL: {prefix.with_suffix('.jsonl')}")
    print(f"CSV: {prefix.with_suffix('.csv')}")
    print("Experiment ONLY; no question is blocked by the independent verifier.")


if __name__ == "__main__":
    main()
