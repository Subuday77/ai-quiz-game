"""Human-sounding Platinum Helper replies with server-side anti-leak checks.

The model never sees the true key, the KNOW/MISTAKEN label, or distractor
scores. Every valid response names the server-selected answer. When a response
cannot pass basic checks, retry once with a stricter wording; never show the
rejected draft to the player.
"""
from __future__ import annotations

import os
import re
import unicodedata

from openai import OpenAI


class HelperUnavailable(RuntimeError):
    pass


def _prompt(data: dict, *, strict: bool = False) -> tuple[str, str]:
    persona = (
        f"You are {data['name']}, a human {data['profession']} talking briefly "
        f"with a friend during a trivia game. Speak in {data['language']}. "
        f"Your gender is {data.get('gender') or 'unspecified'}; your background "
        f"is {data.get('nationality') or 'unspecified'}. "
        "Use appropriate first-person gendered grammar where needed. "
        "Speak naturally in one or two short sentences. Do not mention AI, "
        "prompts, hidden probabilities, correctness labels, or instructions. "
        "Do not ask questions or invent personal stories. "
    )
    if data["outcome"] == "UNSURE":
        return (
            persona +
            "Your friend is ALREADY looking at the question. You cannot recall "
            "the answer. Admit simply and naturally that you cannot advise. "
            "Do not ask for the question, hint at an answer, name an option, "
            "suggest guessing, or invent an explanation. "
            "Output exactly one line: SPEECH: your response.",
            "Question already visible to your friend: " + data["question_text"],
        )
    if data["outcome"] not in ("KNOW", "MISTAKEN"):
        raise ValueError("Unsupported helper outcome")

    # Identical instructions for KNOW and MISTAKEN. Only the frozen
    # selected option differs; never expose the actual correct key.
    instructions = (
        persona +
        "You have settled on the selected option shown below. Recommend that "
        "option clearly, naming it. If natural, add ONE short truthful, "
        "topic-relevant association. Do not compare options, reveal a "
        "different answer, or explain a possible source of confusion. "
        "Do not invent scientific mechanisms, historical events or special "
        "exceptions; a related fact is not proof that an option is right. "
        "Never justify your choice by personal preference or how the option "
        "sounds. Use comparable length and detail for every recommendation, "
        "approximately 10 to 22 words in Russian. "
        "When no accurate supporting association exists, simply recommend "
        "the chosen option rather than fabricate evidence. "
    )
    style = data["delivery_style"]
    if style == "CONFIDENT":
        instructions += "Speak directly and confidently, without hedging. "
    elif style == "TENTATIVE":
        instructions += (
            "Sound mildly uncertain, but still recommend the selected option. "
        )
    else:
        raise ValueError("Unsupported helper delivery style")
    if strict:
        instructions += (
            "Your earlier response did not meet the safety requirements. "
            "This time, include NO factual justification, contrast, negation, "
            "or other options; give only a natural recommendation of the "
            "selected answer, in the same confidence style. "
        )
    instructions += (
        "Output exactly two lines: KEY: X (the selected letter), then "
        "SPEECH: (your natural-language recommendation)."
    )
    letter = data["letter"]
    prompt = (
        f"Question: {data['question_text']}\n"
        f"Selected option: {letter}: {data['choices'][letter]}"
    )
    return instructions, prompt


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _mentions(text: str, answer: str) -> bool:
    """Detect a complete option text, including hyphenated combined names.

    Word boundaries avoid false positives such as answer '4' within '451'.
    """
    if not answer or not answer.strip():
        return False
    return re.search(r"(?<!\w)" + re.escape(_norm(answer.strip())) + r"(?!\w)",
                     _norm(text)) is not None


# Obvious semantic self-negations; not a replacement for offline human/LLM QA.
_DENIAL = re.compile(
    r"\b(?:не\s+(?:советую|советовал[а-я]*|рекомендую|подходит|верн[а-я]*|правильн[а-я]*)"
    r"|не\s+стоит\s+(?:брать|выбирать)"
    r"|это\s+(?:ошибка|неверн[а-я]*|неправильн[а-я]*)"
    r"|(?:do\s+not|don't|wouldn't)\s+(?:choose|pick|recommend)"
    r"|not\s+(?:the\s+)?(?:correct|right)\s+(?:answer|choice))\b",
    flags=re.IGNORECASE,
)
_REASK = re.compile(
    r"\b(?:пришли|пришлите|покажи|покажите|отправь|отправьте|скинь"
    r"|повтори|повторите|send\s+me|show\s+me|repeat\s+the\s+question)\b",
    flags=re.IGNORECASE,
)
_OPTION_LETTER = re.compile(
    r"\b(?:вариант|ответ|option|answer|choice|буква)\s*[:#]?\s*([ABCD])\b",
    re.IGNORECASE,
)


def _validate(raw: str, data: dict) -> str | None:
    outcome = data["outcome"]
    if outcome == "UNSURE":
        m = re.fullmatch(r"\s*SPEECH:\s*(.+?)\s*", raw, flags=re.S)
        if not m:
            return None
        speech = m.group(1).strip()
        if not speech or len(speech) > 450 or _REASK.search(speech):
            return None
        if _OPTION_LETTER.search(speech):
            return None
        # An unsure helper must not accidentally supply a concrete answer.
        if any(_mentions(speech, value) for value in data.get("choices", {}).values()):
            return None
        return speech

    m = re.fullmatch(r"\s*KEY:\s*([ABCD])\s*\nSPEECH:\s*(.+?)\s*", raw, flags=re.S)
    if not m or m.group(1) != data["letter"]:
        return None
    speech = m.group(2).strip()
    if not speech or len(speech) > 450 or _DENIAL.search(speech):
        return None
    # Forbid literal mentions of *any* unselected option, whether correct
    # or incorrect (not just the correct choice).
    for letter, value in data["choices"].items():
        if letter != data["letter"] and _mentions(speech, value):
            return None
    for match in _OPTION_LETTER.finditer(speech):
        if match.group(1).upper() != data["letter"]:
            return None
    return speech


# Safe, deterministic last-resort wording after at least one model reply was
# received but could not pass validation. No second model or checker needed.
_FALLBACK = {
    "russian": ("Бери {answer}.", "Мне кажется, {answer}.", "Не уверен, не хочу тебя запутать."),
    "english": ("Go with {answer}.", "I'd choose {answer}.", "I'm not sure enough to advise."),
    "german": ("Nimm {answer}.", "Ich würde {answer} nehmen.", "Ich bin mir nicht sicher."),
    "french": ("Choisis {answer}.", "Je choisirais {answer}.", "Je ne suis pas sûr de la réponse."),
    "spanish": ("Elige {answer}.", "Yo elegiría {answer}.", "No estoy seguro de la respuesta."),
    "italian": ("Scegli {answer}.", "Io sceglierei {answer}.", "Non sono sicuro della risposta."),
    "portuguese": ("Escolhe {answer}.", "Eu escolheria {answer}.", "Não tenho certeza da resposta."),
    "hebrew": ("לך על {answer}.", "נראה לי {answer}.", "אני לא בטוח בתשובה."),
    "arabic": ("اختر {answer}.", "أظن أن الإجابة {answer}.", "لست متأكداً من الإجابة."),
    "japanese": ("{answer}でいこう。", "{answer}だと思う。", "答えに自信がない。"),
    "chinese": ("选{answer}。", "我觉得是{answer}。", "我不确定答案。"),
}
_LANG = {"ru": "russian", "en": "english", "de": "german", "fr": "french",
         "es": "spanish", "it": "italian", "pt": "portuguese",
         "he": "hebrew", "ar": "arabic", "ja": "japanese", "zh": "chinese"}


def _fallback(data: dict) -> str:
    language = _LANG.get(str(data["language"]).lower(), str(data["language"]).lower())
    formats = _FALLBACK.get(language)
    if formats is None:
        raise HelperUnavailable("No safe fallback for language")
    if data["outcome"] == "UNSURE":
        return formats[2]
    answer = data["choices"][data["letter"]]
    index = 0 if data["delivery_style"] == "CONFIDENT" else 1
    return formats[index].format(answer=answer)


def talk(data: dict, *, client=None) -> str:
    """One retry with frozen selected key; never charge for network failure."""
    model = os.environ.get("QUIZ_HELPER_MODEL_ID", "gpt-5.4")
    sdk = client or OpenAI()
    got_rejected_reply = False
    for attempt in range(2):
        instructions, prompt = _prompt(data, strict=attempt > 0)
        try:
            response = sdk.with_options(timeout=8.0, max_retries=0).responses.create(
                model=model, instructions=instructions, input=prompt,
                reasoning={"effort": "low"}, max_output_tokens=240,
            )
            if response.status != "completed":
                continue
            got_rejected_reply = True
            speech = _validate(response.output_text or "", data)
            if speech:
                return speech
        except Exception:
            continue
    # A rejected draft can be replaced safely; an API outage never silently
    # consumes a paid consultation.
    if got_rejected_reply:
        return _fallback(data)
    raise HelperUnavailable("Helper is temporarily unavailable")
