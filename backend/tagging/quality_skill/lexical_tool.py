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
            if len(word) >= 4 or word == "рим"]


def _prefix_length(left: str, right: str) -> int:
    shared = 0
    for a, b in zip(left, right):
        if a != b:
            break
        shared += 1
    return shared


# Deliberately small set of recognizable Russian prefixes. This detects
# internal-root cues such as "коленной" -> "надколенник", but does NOT
# establish grammatical lemma identity or a definitive answer leak.
_RUSSIAN_PREFIXES = ("над", "под", "без", "бес", "пред", "пере")


def _prefixed_root(stem_word: str, answer_word: str, min_letters: int) -> tuple[int, str]:
    """Return shared root length and leading prefix, or (0, "").

    Require a long, near-complete *stem* root after a recognizable prefix,
    not an arbitrary substring that could occur inside unrelated words.
    Shadow-only: never use this as a publication veto.
    """
    for prefix in _RUSSIAN_PREFIXES:
        if not answer_word.startswith(prefix):
            continue
        shared = _prefix_length(stem_word, answer_word[len(prefix):])
        if shared >= min_letters and shared / len(stem_word) >= 0.75:
            return shared, prefix
    return 0, ""


def inspect(question, settings) -> list[Finding]:
    stem = _tokens(question.question)
    matches: dict[str, tuple[str, str, int, str]] = {}
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
                # Conservative specific inflection: "Римской" in the stem
                # versus the unique "Рим" option is a name clue.
                geographic_root = (
                    answer_word == "рим" and stem_word.startswith("римск")
                )
                prefix = _prefix_length(answer_word, stem_word)
                prefix_match = (
                    prefix >= settings.lexical_min_prefix
                    and prefix / min(len(answer_word), len(stem_word))
                    >= settings.lexical_min_shared_fraction
                )
                internal_length, root_prefix = _prefixed_root(
                    stem_word, answer_word, settings.lexical_min_prefix
                )
                if not (exact_component or geographic_root
                        or prefix_match or internal_length):
                    continue
                common_length = max(prefix, internal_length)
                internal_source = root_prefix if internal_length > prefix else ""
                if best is None or common_length > best[2]:
                    best = (stem_word, answer_word, common_length, internal_source)
        if best is not None:
            matches[key] = best

    # A clue shared by several answers does not reveal one choice.
    if len(matches) != 1:
        return []
    key, (stem_word, answer_word, prefix, internal_source) = next(iter(matches.items()))
    is_keyed = key == question.correct_answer
    exact = stem_word == answer_word
    code = ("unique_exact_name_component" if exact
            else "unique_prefixed_root_shortcut" if internal_source
            else "unique_morphological_shortcut") if is_keyed else "distractor_word_overlap"
    return [Finding(
        tool="lexical",
        code=code,
        severity=(2 if internal_source else 3) if is_keyed else 1,
        confidence=(0.7 if internal_source else 0.92 if exact else 0.82)
        if is_keyed else 0.55,
        evidence={
            "question_token": stem_word,
            "option_token": answer_word,
            "common_prefix": stem_word[:prefix],
            "option": key,
            "matches_correct_option": is_keyed,
            "exact_component": exact,
            "prefix_in_option": internal_source,
            "note": "heuristic shared root; not proof of a shortcut",
        },
    )]
