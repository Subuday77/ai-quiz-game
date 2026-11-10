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
    blind_payload, summarize_answer_review, publication_preview,
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

    # Real questions from user playtesting (31 Oct–1 Nov 2026).
    # PROVISIONAL editorial labels, no effect on the actual publication gate.
    dict(id="jam_only_number_one", level=13,
         stem="Какая песня стала единственным хитом №1 в британском чарте синглов для The Jam?",
         answers=("Going Underground", "Town Called Malice",
                  "Start!", "Beat Surrender"),
         key="A", expected="invalid_premise", accepted=""),
    dict(id="jam_four_number_ones", level=13,
         stem="Какие из перечисленных песен The Jam достигали первого места в британском чарте синглов?",
         answers=("Going Underground", "Town Called Malice",
                  "Start!", "Beat Surrender"),
         key="A", expected="multiple_correct", accepted="ABCD"),
    dict(id="beatles_1967_drums", level=13,
         stem=("Какой хит 1967 года стал единственным синглом группы The Beatles, "
               "на котором ударные сыграл не Ринго Старр, а Пол Маккартни?"),
         answers=("Penny Lane", "The Ballad of John and Yoko",
                  "Back in the U.S.S.R.", "Hello, Goodbye"),
         key="C", expected="invalid_premise", accepted=""),
    dict(id="kate_bush_2022_debut", level=13,
         stem=("Какая песня стала для Кейт Буш первым синглом, дебютировавшим "
               "на первом месте британского чарта после того, как этому "
               "способствовало её использование в сериале "
               "«Очень странные дела» в 2022 году?"),
         answers=("Wuthering Heights", "Cloudbusting",
                  "Running Up That Hill (A Deal with God)", "Babooshka"),
         key="C", expected="invalid_premise", accepted=""),
    dict(id="ginkgo_motile_sperm", level=14,
         stem=("Какое голосеменное растение известно тем, что у него "
               "сохраняются подвижные сперматозоиды, что является редкой "
               "древней чертой среди семенных растений?"),
         answers=("Гинкго двулопастный", "Сосна обыкновенная",
                  "Тис ягодный", "Лиственница европейская"),
         key="A", expected="single_correct", accepted="A"),
    dict(id="vice_city_title_leak", level=4,
         stem=("Как называется вымышленный город, в котором происходит "
               "действие игры Grand Theft Auto: Vice City?"),
         answers=("Либерти-Сити", "Вайс-Сити", "Сан-Андреас", "Лос-Сантос"),
         key="B", expected="single_correct", accepted="B"),
    dict(id="fenrir_odin", level=9,
         stem=("В скандинавской мифологии какой волк, согласно пророчеству, "
               "должен проглотить Одина во время Рагнарёка?"),
         answers=("Гарм", "Сколль", "Фенрир", "Хати"),
         key="C", expected="single_correct", accepted="C"),
    dict(id="riemann_trivial_zeros", level=15,
         stem=("Какое имя носит множество всех действительных чисел x, "
               "для которых функция Римана ζ(x) обращается в нуль "
               "на отрицательных чётных целых значениях?"),
         answers=("Тривиальные нули", "Критическая полоса",
                  "Нетронутый спектр", "Единичные корни"),
         key="A", expected="single_correct", accepted="A"),
    # Frozen verbatim accepted questions in the 2026-10-31 full playthrough.
    # Editorially selected factual positives (NOT automatic live approvals).
    # Include high-score but factually sound questions to measure false rejects.
    dict(id="good_20261031_lava", level=1,
         stem="Как называется расплавленная горная порода, изливающаяся на поверхность Земли во время извержения вулкана?",
         answers=("Магма", "Лава", "Базальт", "Гранит"),
         key="B", expected="single_correct", accepted="B",
         group="playthrough-positive"),
    dict(id="good_20261031_mayakovsky_cloud", level=4,
         stem="Какой поэт написал поэму «Облако в штанах»?",
         answers=("Александр Блок", "Сергей Есенин", "Владимир Маяковский", "Анна Ахматова"),
         key="C", expected="single_correct", accepted="C",
         group="playthrough-positive"),
    dict(id="good_20261031_tape_measure_hook", level=5,
         stem="Для чего на большинстве рулеток есть небольшой металлический крючок на конце измерительной ленты, который слегка двигается?",
         answers=("Чтобы компенсировать толщину крючка при измерении от края или за край", "Чтобы фиксировать ленту на любой цифре без кнопки стопора", "Чтобы защищать ленту от перегиба при сматывании", "Чтобы определять угол наклона поверхности"),
         key="A", expected="single_correct", accepted="A",
         group="playthrough-positive"),
    dict(id="good_20261031_chamonix_1924", level=6,
         stem="Какой город принимал первые зимние Олимпийские игры в 1924 году?",
         answers=("Санкт-Мориц", "Осло", "Шамони", "Гармиш-Партенкирхен"),
         key="C", expected="single_correct", accepted="C",
         group="playthrough-positive"),
    dict(id="good_20261031_lassi_india", level=3,
         stem="Какой молочный напиток традиционно ассоциируется с Индией и часто готовится с йогуртом, водой и специями или фруктами?",
         answers=("Ласси", "Кефир", "Айран", "Ряженка"),
         key="A", expected="single_correct", accepted="A",
         group="playthrough-positive"),
    dict(id="good_20261031_echidna_eggs", level=4,
         stem="Какое млекопитающее откладывает яйца?",
         answers=("Коала", "Ехидна", "Ленивец", "Выдра"),
         key="B", expected="single_correct", accepted="B",
         group="playthrough-positive"),
    dict(id="good_20261031_sudoku_rules", level=6,
         stem="Как называется японская логическая головоломка, в которой нужно заполнить сетку цифрами так, чтобы в каждой строке, каждом столбце и каждом малом квадрате 3×3 числа не повторялись?",
         answers=("Какуро", "Судоку", "Нонограмма", "Хидато"),
         key="B", expected="single_correct", accepted="B",
         group="playthrough-positive"),
    dict(id="good_20261031_guatemala_two_coasts", level=7,
         stem="Какая из этих стран имеет выход и к Карибскому морю, и к Тихому океану?",
         answers=("Гватемала", "Эквадор", "Суринам", "Гайана"),
         key="A", expected="single_correct", accepted="A",
         group="playthrough-positive"),
    dict(id="good_20261031_sagrada_familia_city", level=8,
         stem="В каком городе находится знаменитый храм Святого Семейства (Саграда Фамилия)?",
         answers=("Севилья", "Валенсия", "Барселона", "Мадрид"),
         key="C", expected="single_correct", accepted="C",
         group="playthrough-positive"),
    dict(id="good_20261031_munich_agreement_1938", level=9,
         stem="Какое соглашение 1938 года позволило нацистской Германии присоединить Судетскую область Чехословакии?",
         answers=("Версальский договор", "Пакт Молотова — Риббентропа", "Мюнхенское соглашение", "Локарнские договоры"),
         key="C", expected="single_correct", accepted="C",
         group="playthrough-positive"),
    dict(id="good_20261031_hyoid_unarticulated", level=10,
         stem="Какая кость человека не сочленяется ни с одной другой костью и удерживается в основном мышцами и связками?",
         answers=("Подъязычная кость", "Лопатка", "Грудина", "Ключица"),
         key="A", expected="single_correct", accepted="A",
         group="playthrough-positive"),
    dict(id="good_20261031_cannoli_ricotta", level=12,
         stem="Какой итальянский сыр традиционно используют для начинки десерта канноли по-сицилийски?",
         answers=("Маскарпоне", "Пекорино романо", "Рикотта", "Горгонзола"),
         key="C", expected="single_correct", accepted="C",
         group="playthrough-positive"),
)


# Provisional editorial release decisions, separate from outcome labels.
# A wrong generator key and a literal stem giveaway are unsafe EVEN WHEN
# the four-answer question has exactly one factually correct option.
KNOWN_BLOCK_CASES = frozenset({
    "saturn_wrong_key", "enclave_three_correct", "mrs_dalloway_direct_leak",
    "hanno_periplus", "ebla_direct_leak", "empty_answer_set",
    "incorrect_nickname_premise",
    "jam_only_number_one", "jam_four_number_ones",
    "beatles_1967_drums", "kate_bush_2022_debut",
    "vice_city_title_leak",
})
UNADJUDICATED_CASES = frozenset({"mussorgsky_versions"})


def expected_publication(case_id: str) -> str:
    if case_id in UNADJUDICATED_CASES:
        return "review"
    if case_id in KNOWN_BLOCK_CASES:
        return "block"
    return "allow"


def select_cases(*, ids: list[str] | None = None,
                 limit: int | None = None,
                 group: str | None = None) -> tuple[dict, ...]:
    """Choose a bounded calibration sample without touching the API."""
    if sum(value is not None for value in (ids, limit, group)) > 1:
        raise ValueError("Use only one of ids, limit, group")
    if group is not None:
        if group != "playthrough-positive":
            raise ValueError("Unknown case group: " + group)
        return tuple(case for case in CASES if case.get("group") == group)
    if ids:
        requested = set(ids)
        known = {case["id"] for case in CASES}
        unknown = requested - known
        if unknown:
            raise ValueError("Unknown cases: " + ", ".join(sorted(unknown)))
        return tuple(case for case in CASES if case["id"] in requested)
    if limit is None:
        return CASES
    if not 1 <= limit <= len(CASES):
        raise ValueError(f"limit must be 1..{len(CASES)}")
    return CASES[:limit]


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
    local_leak = exact_answer_leak(q)
    preview = publication_preview(review, q.correct_answer, local_leak)
    release = "block" if preview["would_block"] else "allow"
    expected_release = expected_publication(case["id"])
    return {
        "case": case["id"], "level": case["level"],
        "expected": case["expected"], "observed": summary["outcome"],
        "status_match": summary["outcome"] == case["expected"],
        "expected_choices": case["accepted"], "observed_choices": predicted,
        "choices_match": predicted == case["accepted"] if case["accepted"] else None,
        "generator_key": q.correct_answer, "key_supported": q.correct_answer in predicted,
        "local_leak": local_leak,
        "release_preview": release, "release_reasons": preview["reasons"],
        "expected_release": expected_release,
        "release_match": (release == expected_release if expected_release != "review" else None),
        "per_option": {k: v.model_dump(mode="json") for k, v in review.options.items()},
        "premise": review.premise.value, "rationale": review.rationale,
        "duration_seconds": round(time.perf_counter() - start, 3),
        "input_tokens": response.usage.input_tokens if response.usage else None,
        "output_tokens": response.usage.output_tokens if response.usage else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int,
                        help="First N frozen cases (default: all)")
    parser.add_argument("--case", action="append", dest="case_ids",
                        help="Run one named case; repeat this flag for several")
    parser.add_argument("--group", choices=["playthrough-positive"],
                        help="Run all 12 factual positives from the 2026-10-31 playthrough")
    parser.add_argument("--list", action="store_true",
                        help="List case IDs without sending any API request")
    parser.add_argument("--repeats", type=int, default=1,
                        help="Run each selected case N times (1..5, billable)")
    args = parser.parse_args()
    if args.list:
        for case in CASES:
            print(f"{case['id']:<40} {case['expected']}"
                  + (" [playthrough-positive]" if case.get("group") else ""))
        return
    if sum(value is not None for value in (args.case_ids, args.limit, args.group)) > 1:
        parser.error("Use only one of --case, --limit or --group")
    if not 1 <= args.repeats <= 5:
        parser.error("--repeats must be 1..5")
    try:
        selected = select_cases(ids=args.case_ids, limit=args.limit,
                                group=args.group)
    except ValueError as exc:
        parser.error(str(exc))
    key = os.getenv("OPENAI_API_KEY")
    if not key or key.startswith("REPLACE_"):
        parser.error("Set OPENAI_API_KEY in the root .env before running")
    client = OpenAI(api_key=key, timeout=30, max_retries=0)
    prefix = ROOT / f"answer_check_{datetime.now():%Y%m%d_%H%M%S}"
    rows = []
    print(f"KEY-BLIND ANSWER CHECK | {MODEL} | {len(selected)} cases "
          f"x {args.repeats} = {len(selected) * args.repeats} billable calls")
    total = len(selected) * args.repeats
    for i, (case, repeat) in enumerate(
            ((case, trial) for case in selected
             for trial in range(1, args.repeats + 1)), 1):
        try:
            row = run_case(client, case)
        except Exception as exc:
            row = {"case": case["id"], "expected": case["expected"],
                   "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        row["trial"] = repeat
        rows.append(row)
        option_status = (
            " ".join(f"{letter}:{row['per_option'][letter]['status']}"
                     for letter in "ABCD")
            if "per_option" in row else "not available"
        )
        print(f"[{i}/{total}] {case['id']} (trial {repeat}) "
              f"| expected={case['expected']} "
              f"| observed={row.get('observed', 'ERROR')} "
              f"| choices={row.get('observed_choices', '?')} "
              f"| A-D={option_status} "
              f"| PREVIEW={row.get('release_preview', 'error')}")
        if "error" in row:
            print(f"    ERROR {row['error']}")
    with prefix.with_suffix(".jsonl").open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    fields = ["case", "trial", "level", "expected", "observed", "status_match",
              "expected_choices", "observed_choices", "choices_match",
              "generator_key", "key_supported", "local_leak", "premise",
              "release_preview", "release_reasons", "expected_release", "release_match",
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
    print(f"SINGLE-CORRECT CLASS MATCH: {sum(r['status_match'] for r in good)}/{len(good)}"
          " (NOT a publication-safety score; may contain wrong keys/leaks)")
    print(f"NONSINGLE CLASS DETECTED: {sum(r['observed'] != 'single_correct' for r in flawed)}/{len(flawed)}")
    release_scored = [r for r in valid if r["expected_release"] != "review"]
    good_release = [r for r in release_scored if r["expected_release"] == "allow"]
    defective = [r for r in release_scored if r["expected_release"] == "block"]
    print(f"HYPOTHETICAL SAFE QUESTIONS ACCEPTED: "
          f"{sum(r['release_preview'] == 'allow' for r in good_release)}/{len(good_release)}")
    print(f"HYPOTHETICAL KNOWN DEFECTS BLOCKED: "
          f"{sum(r['release_preview'] == 'block' for r in defective)}/{len(defective)}")
    for row in valid:
        if row["expected_release"] == "block" and row["release_preview"] != "block":
            print(f"MISSED KNOWN DEFECT: {row['case']}")
        if row["expected_release"] == "allow" and row["release_preview"] != "allow":
            print(f"FALSE REJECTION: {row['case']}")
        if row["expected_release"] == "review":
            print(f"UNADJUDICATED: {row['case']} | preview={row['release_preview']}")
    print("Preview combines the local literal gate, independent answer validity, "
          "and generator-key agreement. NOT enabled for actual games.")
    print(f"JSONL: {prefix.with_suffix('.jsonl')}")
    print(f"CSV: {prefix.with_suffix('.csv')}")
    print("Experiment ONLY; no question is blocked by the independent verifier.")


if __name__ == "__main__":
    main()
