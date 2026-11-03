"""Known QA-regression items: do not recycle unchanged problematic questions.

Exact question+keyed-option matching ONLY. This does not assert that nearby
facts are false, and does not mutate stored data, previous player history,
quality decisions or awards. Retire these matches from the approved global
fallback until there is a full player-report adjudication workflow.
"""
import unicodedata


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


# QA report: AI_Quiz_Game_P1_Final_Live_QA(1).md, 2026-11-03.
# We exclude the exact BAD FORMULATION, not all questions about this topic.
_KNOWN_ITEMS = [
    ("calvados_ambiguous_apple_pear",
     "Из какого фрукта традиционно делают кальвадос?", "Из яблока"),
    ("savarin_surname_shortcut",
     "Какой французский гастроном и автор книги «Physiologie du goût» считается человеком, в честь которого названо блюдо саварен?",
     "Жан Антельм Брийя-Саварен"),
    ("roman_empire_rome_name_shortcut",
     "Какой город был столицей Древней Римской империи?", "Рим"),
    ("hua_rong_dao_unverified_origin",
     "Какой из этих старинных позиционных головоломок традиционно считается происходящим из китайской игры «Хуа Жун Дао»?",
     "Игра в 15 солдат"),
    ("puerto_williams_wrong_airport_name",
     "Какой аэропорт является самым южным в мире среди аэропортов с регулярным коммерческим пассажирским сообщением?",
     "Аэропорт имени гвардейского капитана Омара Дарьена, Пуэрто-Уильямс"),
    ("thimphu_unsupported_river",
     "Столица какого государства расположена на берегах реки Чуруйунгбо и известна как одна из самых высокогорных столиц мира?",
     "Бутан"),
]
_INDEX = {(_fold(stem), _fold(answer)): code
          for code, stem, answer in _KNOWN_ITEMS}


def known_qa_issue(question) -> str | None:
    try:
        answer = question.answers[question.correct_answer]
        return _INDEX.get((_fold(question.question), _fold(answer)))
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
