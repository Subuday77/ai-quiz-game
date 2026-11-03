"""Find one-option morphological shortcuts; never reject on heuristics.

This is NOT grammatical lemmatization and not a semantic dictionary. It looks
for unusually long shared prefixes in the question and precisely one option,
including 'циркония' -> 'циркон'. Full literal leakage is already handled by
the independent existing local_leak_filter hard gate.
"""
import re
import unicodedata

from .types import Finding


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return [word for word in re.findall(r"[^\W\d_]+", normalized, re.UNICODE)
            if len(word) >= 4]


def _prefix_length(left: str, right: str) -> int:
    shared = 0
    for a, b in zip(left, right):
        if a != b:
            break
        shared += 1
    return shared


def inspect(question, settings) -> list[Finding]:
    stem = _tokens(question.question)
    matches: dict[str, tuple[str, str, int]] = {}
    for key, text in question.answers.items():
        best = None
        for answer_word in _tokens(text):
            for stem_word in stem:
                # A whole answer may not repeat the question, but a component
                # of a hyphenated surname CAN be the entire hidden clue:
                # 'саварен' -> 'Брийя-Саварен'. The old exact-word skip missed
                # this, while exact whole-choice leaks have a separate gate.
                exact_component = (answer_word == stem_word
                                   and len(answer_word) >= 5)
                prefix = _prefix_length(answer_word, stem_word)
                if not exact_component and (
                    prefix < settings.lexical_min_prefix
                    or prefix / min(len(answer_word), len(stem_word))
                    < settings.lexical_min_shared_fraction
                ):
                    continue
                if best is None or prefix > best[2]:
                    best = (stem_word, answer_word, prefix)
        if best is not None:
            matches[key] = best

    # A clue shared by several answers does not reveal one choice.
    if len(matches) != 1:
        return []
    key, (stem_word, answer_word, prefix) = next(iter(matches.items()))
    is_keyed = key == question.correct_answer
    exact = stem_word == answer_word
    return [Finding(
        tool="lexical",
        code=("unique_exact_name_component" if exact else "unique_morphological_shortcut")
        if is_keyed else "distractor_word_overlap",
        severity=3 if is_keyed else 1,
        confidence=(0.92 if exact else 0.82) if is_keyed else 0.55,
        evidence={
            "question_token": stem_word,
            "option_token": answer_word,
            "common_prefix": stem_word[:prefix],
            "option": key,
            "matches_correct_option": is_keyed,
            "exact_component": exact,
            "note": "heuristic shared root; not proof of a shortcut",
        },
    )]
