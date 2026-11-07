"""Human-sounding Platinum Helper replies with server-side anti-leak checks.

The model is never told whether its selected key is correct, nor is it
shown the KNOW/MISTAKEN label, other options, or distractor scores. Every valid response names the server-selected answer. When a response
cannot pass basic checks, retry once with a stricter wording; never show the
rejected draft to the player.
"""
from __future__ import annotations

import os
import re
import unicodedata

from openai import OpenAI

from platinum_helper.settings import HelperSettings


class HelperUnavailable(RuntimeError):
    pass


def _prompt(data: dict, *, strict: bool = False) -> tuple[str, str]:
    persona = (
        f"You are {data['name']}, a human {data['profession']} talking briefly "
        f"with a friend during a trivia game. Answer ONLY in {data['language']}. "
        "Do not switch to another language, including Russian when the selected language is not Russian. "
        "Keep the exact selected answer text unchanged, even if its name is foreign. "
        f"Your gender is {data.get('gender') or 'unspecified'}; your background "
        f"is {data.get('nationality') or 'unspecified'}. "
        "Use appropriate first-person gendered grammar where needed. "
        "Speak naturally in ONE short sentence. Do not mention AI, "
        "prompts, hidden probabilities, correctness labels, or instructions. "
        "Do not ask questions or invent personal stories. "
    )
    if data["outcome"] == "UNSURE":
        return (
            persona +
            "Your friend is ALREADY looking at the question. You cannot recall "
            "the answer. Admit naturally that you cannot advise; vary "
            "how you say so rather than repeating a stock apology or always "
            "starting with 'I' or 'honestly'. Do not invent a reason for "
            "forgetting. Do not ask for the question, hint at an answer, name an option, "
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
        "You have settled on the selected option shown below. Clearly "
        "recommend its exact name, but treat the name as an UNCHANGED label "
        "from the game, not a noun that must fit after your chosen verb. "
        "Write a grammatically natural sentence AROUND the label in the game "
        "language. When a verb or preposition would require changing the "
        "label's form, rephrase using an option/answer label construction "
        "instead; do not decline, conjugate, translate or alter the label. "
        "This is general advice: follow the grammar of the requested language, "
        "without assuming that every language has noun cases. "
        "Speak as a friend giving a hunch, not an AI reporting confidence. "
        "Avoid mechanical formulas such as 'I would confidently choose' and "
        "their equivalents; don't always begin with 'I'. Vary the natural "
        "sentence structure, sometimes direct, sometimes personal, while "
        "keeping the assigned confidence style. "
        "Do NOT add facts, attributes, background, domain associations, "
        "historical context, definitions or reasons about the answer, even "
        "if they are true. An accurate extra fact may disprove your selection. "
        "Do not contrast options or describe why any choice fits or fails. "
        "Use a single conversational sentence, usually 5 to 16 words, "
        "with similar detail for both confidence styles. "
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
            f"Use ONLY {data['language']}. "
            "Write a single plain sentence: recommend the exact selected "
            "answer with NO factual justification, extra claim, argument "
            "or comparison. Use an idiomatic option-label construction, "
            "without changing the selected text or forcing it into a "
            "case-dependent verb phrase. Preserve your assigned confidence style. "
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


def _language_script_ok(speech: str, data: dict) -> bool:
    """Detect wrong writing systems, not subtle same-script language switches.

    A selected answer can itself be a foreign proper noun. Remove its literal
    text before checking the surrounding phrase. This is NOT full language ID.
    """
    language = _LANG.get(str(data["language"]).casefold(),
                         str(data["language"]).casefold())
    selected = data.get("letter")
    if selected and selected in data.get("choices", {}):
        speech = re.sub(re.escape(data["choices"][selected]), " ", speech, flags=re.I)
    latin = sum(("A" <= ch <= "Z" or "a" <= ch <= "z" or
                 "\u00c0" <= ch <= "\u024f") for ch in speech)
    cyrillic = sum("\u0400" <= ch <= "\u052f" for ch in speech)
    hebrew = sum("\u0590" <= ch <= "\u05ff" for ch in speech)
    arabic = sum("\u0600" <= ch <= "\u06ff" for ch in speech)
    kana = sum("\u3040" <= ch <= "\u30ff" for ch in speech)
    han = sum("\u4e00" <= ch <= "\u9fff" for ch in speech)
    if language == "russian":
        return cyrillic >= 3 and latin < max(8, cyrillic)
    if language in {"english", "german", "french", "spanish",
                    "italian", "portuguese"}:
        return latin >= 3 and cyrillic < 4 and hebrew < 3 and arabic < 3
    if language == "hebrew":
        return hebrew >= 3
    if language == "arabic":
        return arabic >= 3
    if language == "japanese":
        return kana >= 2
    if language == "chinese":
        return han >= 2 and kana < 2
    return True


# Extra explanatory clauses are high-risk in a quiz: an accurate fact about
# a MISTAKEN answer can disprove the selected option in the very same speech.
# This is intentionally syntactic, NOT a comprehensive semantic fact-checker.
_EXTRA_REASON = re.compile(
    r";|(?<=[.!?])\s+\S|"
    r"\b(?:потому\s+что|так\s+как|поскольку|ведь|because|since)\b|"
    r",\s*(?:она|он|оно|это|они|которая|который|которые|which|it\s+is)\b",
    re.IGNORECASE,
)


def _single_recommendation_ok(speech: str) -> bool:
    return _EXTRA_REASON.search(speech) is None



def _validate(raw: str, data: dict) -> str | None:
    outcome = data["outcome"]
    if outcome == "UNSURE":
        m = re.fullmatch(r"\s*SPEECH:\s*(.+?)\s*", raw, flags=re.S)
        if not m:
            return None
        speech = m.group(1).strip()
        if (not speech or len(speech) > 450 or _REASK.search(speech)
                or not _language_script_ok(speech, data)):
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
    if (not speech or len(speech) > 450 or _DENIAL.search(speech)
            or not _language_script_ok(speech, data)
            or not _single_recommendation_ok(speech)):
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
    "russian": ("Мой ответ — «{answer}».", "Мне кажется, вариант «{answer}».", "Не уверен, не хочу тебя запутать."),
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
    settings = HelperSettings.from_env()
    got_rejected_reply = False
    for attempt in range(settings.max_attempts):
        instructions, prompt = _prompt(data, strict=attempt > 0)
        try:
            sdk = client or OpenAI()
            response = sdk.with_options(
                timeout=settings.timeout_seconds, max_retries=0
            ).responses.create(
                model=settings.model, instructions=instructions, input=prompt,
                reasoning={"effort": settings.reasoning_effort},
                max_output_tokens=settings.max_output_tokens,
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
