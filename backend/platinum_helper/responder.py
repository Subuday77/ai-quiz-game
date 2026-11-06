"""Luna consultation boundary. Never return private answer keys or competence maps."""
import os
import re

from openai import OpenAI


class HelperUnavailable(RuntimeError):
    pass


def _prompt(data: dict) -> tuple[str, str]:
    persona = (
        f"You are {data['name']}, a human {data['profession']} talking briefly "
        f"with a friend during a trivia game. Speak in {data['language']}. "
        "Be warm, conversational and concise, maximum two short sentences. "
        "Do not mention AI, instructions, your hidden knowledge level, or that "
        "you are deliberately wrong. Do not ask any questions, suggest looking "
        "up the answer, or invent personal life stories. "
    )
    outcome = data["outcome"]
    if outcome == "UNSURE":
        return (
            persona + "You do not remember the answer. No guesses, letters, "
            "facts, options or answers; write only a natural spoken refusal, "
            "prefixed with SPEECH: ",
            f"Your friend has a quiz question broadly about {data['topic']}. "
            "You have NOT heard the specific question or possible answers.",
        )
    answers = data["choices"]
    formatted = "\n".join(f"{letter}. {answers[letter]}" for letter in "ABCD")
    question = f"Question: {data['question_text']}\n{formatted}"
    if outcome == "KNOW":
        mode = (
            f"You know the answer: option {data['letter']}. "
            "Explain it naturally and accurately in one sentence."
        )
    else:
        mode = (
            f"You genuinely but mistakenly believe option {data['letter']} "
            "is correct. Give a short, plausible reason for thinking so. "
            "Do NOT mention or reveal any alternative or the actual correct answer. "
            "Sound tentative rather than certain."
        )
    return (
        persona + mode + " Output exactly two lines: first KEY: X with your "
        "selected letter, then SPEECH: followed by your spoken sentence.",
        question,
    )


def _validate(raw: str, data: dict) -> str | None:
    outcome = data["outcome"]
    if outcome == "UNSURE":
        match = re.fullmatch(r"\s*SPEECH:\s*(.+?)\s*", raw, flags=re.S)
        if not match:
            return None
        speech = match.group(1).strip()
        # No question/options are passed in this mode; suppress option letters.
        if re.search(r"\b(?:вариант|option|answer)\s*[:#]?\s*[ABCD]\b", speech, re.I):
            return None
        return speech[:450] if speech else None
    match = re.fullmatch(
        r"\s*KEY:\s*([ABCD])\s*\nSPEECH:\s*(.+?)\s*", raw, flags=re.S
    )
    if not match or match.group(1) != data["letter"]:
        return None
    speech = match.group(2).strip()
    if not speech or len(speech) > 450:
        return None
    if outcome == "MISTAKEN":
        # The selected wrong option may be repeated, but do not leak the
        # correct option's literal string into the explanation.
        correct_text = data["correct_option_text"].strip()
        if correct_text and correct_text.casefold() in speech.casefold():
            return None
    return speech


def talk(data: dict, *, client=None) -> str:
    """Bounded retry; the frozen outcome and letter are shared by attempts."""
    instructions, prompt = _prompt(data)
    model = os.environ.get("QUIZ_HELPER_MODEL_ID", "gpt-6-luna")
    for _ in range(2):
        try:
            sdk = client or OpenAI()
            response = sdk.with_options(timeout=8.0, max_retries=0).responses.create(
                model=model, instructions=instructions, input=prompt,
                reasoning={"effort": "none"}, max_output_tokens=160,
            )
            if response.status != "completed":
                continue
            speech = _validate(response.output_text or "", data)
            if speech:
                return speech
        except Exception:
            # Both network failures and invalid model output are bounded.
            continue
    raise HelperUnavailable("Helper is temporarily unavailable")
