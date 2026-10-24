"""Standalone, low-cost live benchmark for the audit-only Critic v3 rubric.

From project root after git pull:
    python test_openai_critic_v3_calibration.py
    python test_openai_critic_v3_calibration.py --real-game

Sends only the question and requested difficulty to the model.
Human expectations are held out locally for evaluation, not injected into
the prompt or the candidate message. Does not start a game or affect production.
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

from agents.critic_v3 import QUALITY_REVIEW_V3_PROMPT
from question_schema import QuizQuestion
from quality_schema_v3 import QualityReviewV3, audit_score, quality_review_v3_schema

MODEL = "gpt-5.4-mini"
REASONING_EFFORT = "low"
TEMPERATURE = 1.0
MAX_OUTPUT_TOKENS = 1500
TIMEOUT = 30

# 'leak' = expected critical textual clue; 'min_d'/'max_d' are tentative
# *editorial bands*, not hard truth about player performance.
# The same stem is reused at different levels to isolate the distractors.
CASES = (
    dict(id="piano_easy", difficulty=0, category="Music",
         stem="На каком музыкальном инструменте обычно играют с помощью клавиш?",
         answers=("Скрипка", "Флейта", "Пианино", "Барабан"), key="C",
         leak="pass", min_d=0, max_d=0),
    dict(id="hola_near_easy", difficulty=1, category="Languages",
         stem="В каком языке слово «hola» используется как приветствие?",
         answers=("Испанский", "Португальский", "Немецкий", "Финский"), key="A",
         leak="pass", min_d=2, max_d=3),
    dict(id="hola_far_easy", difficulty=1, category="Languages",
         stem="В каком языке слово «hola» используется как приветствие?",
         answers=("Испанский", "Финский", "Немецкий", "Русский"), key="A",
         leak="pass", min_d=0, max_d=1),
    dict(id="saturn_easy", difficulty=1, category="Astronomy",
         stem="Какая планета Солнечной системы особенно известна своими хорошо заметными кольцами?",
         answers=("Марс", "Сатурн", "Венера", "Меркурий"), key="B",
         leak="pass", min_d=0, max_d=1),
    dict(id="saturn_famous_medium", difficulty=5, category="Astronomy",
         stem="Какая планета Солнечной системы особенно известна своими хорошо заметными кольцами?",
         answers=("Юпитер", "Сатурн", "Уран", "Нептун"), key="B",
         leak="pass", min_d=1, max_d=3),
    dict(id="gutenberg_medium", difficulty=4, category="History",
         stem="С каким изобретением в Европе XV века связано имя Иоганна Гутенберга?",
         answers=("Паровая машина", "Печатный станок", "Телефон", "Микроскоп"), key="B",
         leak="pass", min_d=1, max_d=2),
    dict(id="gutenberg_high", difficulty=8, category="History",
         stem="С каким изобретением в Европе XV века связано имя Иоганна Гутенберга?",
         answers=("Паровая машина", "Печатный станок", "Телефон", "Микроскоп"), key="B",
         leak="pass", min_d=2, max_d=3),
    dict(id="slogan_attribution", difficulty=6, category="TV shows",
         stem="С каким телесериалом связан слоган «The truth is out there»?",
         answers=("Остаться в живых", "Секретные материалы", "Грань", "Твин Пикс"), key="B",
         leak="pass", min_d=0, max_d=1),
    dict(id="impression_leak", difficulty=6, category="Visual arts",
         stem="Какое художественное направление получило своё название от французского слова impression («впечатление»)?",
         answers=("Фовизм", "Импрессионизм", "Кубизм", "Сюрреализм"), key="B",
         leak="fail", min_d=0, max_d=3),
    dict(id="parchment_reasoning", difficulty=4, category="Ancient history",
         stem="Какой древний город дал название материалу для письма из обработанной кожи животных?",
         answers=("Милет", "Пергам", "Эфес", "Смирна"), key="B",
         leak="pass", min_d=0, max_d=1),
    dict(id="hanami_challenging", difficulty=7, category="Japanese culture",
         stem="Как называется японская традиция любования цветением сакуры?",
         answers=("Танабата", "Ханами", "Сэцубун", "Обон"), key="B",
         leak="pass", min_d=0, max_d=1),
    dict(id="elephant_high_options", difficulty=8, category="Animals",
         stem="Какое из перечисленных наземных животных обычно достигает наибольшей массы тела?",
         answers=("Африканский саванный слон", "Азиатский слон",
                  "Белый носорог", "Бегемот"), key="A",
         leak="pass", min_d=0, max_d=1),
)


# Faithful transcription of the 8 screenshots from the 2026-10-22 game.
# These cases NEVER appear in model instructions. Ranges are provisional
# human editorial expectations, not a ground-truth measure of player ability.
REAL_GAME_CASES = (
    dict(id="game_01_piano", difficulty=0, category="Music",
         stem="На каком музыкальном инструменте обычно играют с помощью клавиш?",
         answers=("Скрипка", "Флейта", "Пианино", "Барабан"), key="C",
         leak="pass", min_d=0, max_d=0),
    dict(id="game_02_hola", difficulty=1, category="Languages",
         stem="На каком языке слово «hola» используется как обычное приветствие?",
         answers=("Итальянский", "Португальский", "Испанский", "Румынский"), key="C",
         leak="pass", min_d=2, max_d=3),
    dict(id="game_03_whale", difficulty=2, category="Animals",
         stem="Какое из этих животных является самым крупным из ныне живущих на Земле?",
         answers=("Африканский слон", "Синий кит", "Белый медведь", "Жираф"), key="B",
         leak="pass", min_d=0, max_d=0),
    dict(id="game_04_gearbox", difficulty=3, category="Technology",
         stem="Какой механизм в автомобиле обычно используется для увеличения крутящего момента при трогании с места и движении на низкой скорости?",
         answers=("Дифференциал", "Коробка передач", "Генератор", "Радиатор"), key="B",
         leak="pass", min_d=2, max_d=3),
    dict(id="game_05_gutenberg", difficulty=4, category="History",
         stem="Какое изобретение обычно приписывают Иоганну Гутенбергу в Европе XV века?",
         answers=("Паровую машину", "Печатный станок с подвижными литерами",
                  "Телефон", "Микроскоп"), key="B",
         leak="pass", min_d=1, max_d=2),
    dict(id="game_06_saturn", difficulty=5, category="Astronomy",
         stem="Какая планета Солнечной системы имеет самые большие по площади кольца?",
         answers=("Юпитер", "Сатурн", "Уран", "Нептун"), key="B",
         leak="pass", min_d=1, max_d=3),
    dict(id="game_07_xfiles", difficulty=6, category="Television",
         stem="Какой телесериал впервые сделал фразу «The truth is out there» широко известным слоганом?",
         answers=("Twin Peaks", "The X-Files", "Lost", "Fringe"), key="B",
         leak="pass", min_d=0, max_d=1),
    dict(id="game_08_hanami", difficulty=7, category="Japanese culture",
         stem="Какой японский праздник традиционно связан с любованием цветением сакуры?",
         answers=("Танабата", "Ханами", "Сэцубун", "Обон"), key="B",
         leak="pass", min_d=0, max_d=1),
)


def question_from_case(case):
    return QuizQuestion.model_validate({
        "difficulty": case["difficulty"], "question": case["stem"],
        "answers": dict(zip("ABCD", case["answers"])),
        "correct_answer": case["key"],
    })


def candidate_payload(question):
    """Never include gold labels, scores, case IDs, or expected decisions."""
    return {"requested_difficulty": question.difficulty,
            "language": "Russian", "candidate": question.model_dump()}


def run_case(client, case):
    question = question_from_case(case)
    started = time.perf_counter()
    response = client.responses.create(
        model=MODEL,
        instructions=QUALITY_REVIEW_V3_PROMPT,
        input=json.dumps(candidate_payload(question), ensure_ascii=False),
        reasoning={"effort": REASONING_EFFORT},
        temperature=TEMPERATURE,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_schema", "name": "quality_review_v3",
                          "strict": True, "schema": quality_review_v3_schema()}},
    )
    if response.status != "completed" or not response.output_text:
        raise RuntimeError(f"Reviewer response incomplete: {response.status}")
    report = QualityReviewV3.model_validate_json(response.output_text)
    score = audit_score(report, question.correct_answer)
    usage = response.usage
    return {
        "case": case["id"], "difficulty": case["difficulty"],
        "leak_expected": case["leak"], "leak_observed": report.leakage.value,
        "leak_match": report.leakage.value == case["leak"],
        "distractor_expected_min": case["min_d"],
        "distractor_expected_max": case["max_d"],
        "distractor_observed": report.distractor_penalty,
        "distractor_band_match": case["min_d"] <= report.distractor_penalty <= case["max_d"],
        "penalties": score["penalties"], "score": score["total"],
        "critical": score["critical"], "difficulty_rating": report.difficulty.value,
        "issue_codes": [x.value for x in report.issue_codes],
        "rationale": report.rationale, "elapsed_seconds": round(time.perf_counter() - started, 2),
        "input_tokens": usage.input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
    }


def main():
    real_game = "--real-game" in sys.argv[1:]
    unknown = set(sys.argv[1:]) - {"--real-game"}
    if unknown:
        raise SystemExit(f"Unknown arguments: {sorted(unknown)}")
    cases = REAL_GAME_CASES if real_game else CASES
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY not set in the environment or project .env")
    client = OpenAI(api_key=key, timeout=TIMEOUT, max_retries=0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = "critic_v3_real_game" if real_game else "critic_v3_calibration"
    jsonl_path = ROOT / f"{prefix}_{stamp}.jsonl"
    csv_path = ROOT / f"{prefix}_{stamp}.csv"
    rows = []
    print(f"CRITIC v3 CALIBRATION | model {MODEL} | {len(cases)} cases")
    print("=" * 72)
    for i, case in enumerate(cases, 1):
        try:
            row = run_case(client, case)
        except Exception as exc:
            row = {"case": case["id"], "error": f"{type(exc).__name__}: {str(exc)[:250]}",
                   "leak_match": False, "distractor_band_match": False}
        rows.append(row)
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{i}/{len(cases)}] {row['case']}: "
              f"leak {row.get('leak_observed', 'ERROR')} / {case['leak']} "
              f"| distractors {row.get('distractor_observed', '?')} / "
              f"{case['min_d']}..{case['max_d']}"
              f"| total {row.get('score', '?')} | critical {row.get('critical', '?')}")
        if row.get("rationale"):
            print(f"  WHY: {row['rationale']}")
        if row.get("error"):
            print(f"  ERROR: {row['error']}")
    fields = ["case", "difficulty", "leak_expected", "leak_observed", "leak_match",
              "distractor_expected_min", "distractor_expected_max", "distractor_observed",
              "distractor_band_match", "score", "critical", "difficulty_rating",
              "penalties", "issue_codes", "rationale", "elapsed_seconds",
              "input_tokens", "output_tokens", "error"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    ok = [r for r in rows if "error" not in r]
    print("\n" + "=" * 72)
    print(f"Successful: {len(ok)}/{len(rows)}")
    print(f"LEAKAGE MATCH: {sum(r['leak_match'] for r in ok)}/{len(ok)}")
    print(f"DISTRACTOR BAND MATCH: {sum(r['distractor_band_match'] for r in ok)}/{len(ok)}")
    if len(ok) == len(cases) and not real_game:
        by_id = {r["case"]: r["distractor_observed"] for r in ok}
        pairs = [
            ("hola_near_easy", "hola_far_easy", ">"),
            ("saturn_famous_medium", "saturn_easy", ">"),
            ("gutenberg_high", "gutenberg_medium", ">="),
        ]
        for hard, easy, op in pairs:
            matched = (by_id[hard] > by_id[easy] if op == ">" else
                       by_id[hard] >= by_id[easy])
            print(f"CONTRAST {hard} {op} {easy}: {matched} "
                  f"({by_id[hard]} vs {by_id[easy]})")
    print(f"JSONL: {jsonl_path}\nCSV: {csv_path}")


if __name__ == "__main__":
    main()
