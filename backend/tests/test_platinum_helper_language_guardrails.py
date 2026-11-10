"""Offline tests for incorrect-language advice and rationale disqualification.

Regression cases are distilled from the first 30 real GPT-5.4 consultations.
No paid model calls or database access.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helper_fallback_fixtures import english_templates
from platinum_helper.responder import _prompt, _validate, talk


def question(*, language="Russian", outcome="MISTAKEN", selected="B",
             choices=None):
    return {
        "name": "Alex", "profession": "Historian",
        "language": language, "outcome": outcome,
        "delivery_style": "TENTATIVE", "letter": selected,
        "question_text": "Какой вариант лучше?" if language == "Russian" else "Which one?",
        "choices": choices or {
            "A": "Афина", "B": "Афродита", "C": "Гера", "D": "Артемида",
        },
        "correct_option_text": "Афина",
    }


class FakeClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = 0
        self.requests = []
        self.responses = self

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        return SimpleNamespace(status="completed", output_text=next(self.outputs))


class GuardrailTests(unittest.TestCase):
    def test_prompt_same_for_know_and_mistaken_and_language_specific(self):
        wrong = question(language="English", choices={
            "A": "Stage", "B": "Orchestra pit",
            "C": "Balcony", "D": "Auditorium",
        })
        know = dict(wrong, outcome="KNOW", letter="A")
        i1, _ = _prompt(wrong)
        i2, _ = _prompt(know)
        self.assertEqual(i1, i2)
        self.assertIn("ONLY in English", i1)
        self.assertIn("Do NOT add facts", i1)
        self.assertNotIn("words in Russian", i1)
        ru_instruction, _ = _prompt(question())
        self.assertIn("ONLY in Russian", ru_instruction)

    def test_english_question_rejects_russian_speech(self):
        d = question(language="English", choices={
            "A": "Stage", "B": "Orchestra pit",
            "C": "Balcony", "D": "Auditorium",
        })
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Думаю, стоит выбрать Orchestra pit.", d
        ))
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: I'd lean toward Orchestra pit.", d
        ), "I'd lean toward Orchestra pit.")

    def test_russian_question_rejects_english_speech(self):
        d = question()
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: I would choose Афродита.", d
        ))
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: Я бы, пожалуй, выбрал Афродита.", d
        ), "Я бы, пожалуй, выбрал Афродита.")

    def test_wrong_script_rejected_in_unsure_too(self):
        d = question(language="English", outcome="UNSURE",
                     selected=None, choices={"A": "Stage", "B": "Pit",
                                             "C": "Balcony", "D": "Hall"})
        self.assertIsNone(_validate("SPEECH: Не знаю, прости.", d))
        self.assertEqual(_validate("SPEECH: I honestly don't know.", d),
                         "I honestly don't know.")

    def test_all_four_observed_disqualification_patterns(self):
        theatre = question(language="Russian", choices={
            "A": "сцена", "B": "оркестровая яма",
            "C": "балкон", "D": "фойе",
        })
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Я бы выбрал оркестровая яма; она перед сценой.", theatre))
        god = question()
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Афродита. Она богиня любви и красоты.", god))
        sea = question(choices={"A": "Иордания", "B": "Словения",
                                "C": "Албания", "D": "Марокко"})
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Выбирай Словения. Адриатика связана со Средиземным морем.", sea))
        food = question(choices={"A": "Кимчи", "B": "Гочуджан",
                                 "C": "Кимбап", "D": "Булгоги"})
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Я бы выбрал Гочуджан; это ферментированная паста.", food))

    def test_know_same_explanation_guard_as_mistaken(self):
        d = question(outcome="KNOW", selected="A")
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: Выбирай Афина. Она богиня мудрости.", d))
        self.assertEqual(_validate(
            "KEY: A\nSPEECH: Мне кажется, Афина.", d
        ), "Мне кажется, Афина.")

    def test_wrong_script_uses_single_call_then_safe_template_with_same_key(self):
        d = question(language="English", choices={
            "A": "Stage", "B": "Orchestra pit",
            "C": "Balcony", "D": "Auditorium",
        })
        client = FakeClient([
            "KEY: B\nSPEECH: Я бы выбрал Orchestra pit.",
            "KEY: B\nSPEECH: I'd go with Orchestra pit.",
        ])
        with patch("platinum_helper.responder.random.SystemRandom") as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[0]
            self.assertEqual(talk(d, client=client, fallback_loader=english_templates), "I think it's Orchestra pit.")
        self.assertEqual(client.calls, 1)
        self.assertIn("ONLY in English", client.requests[0]["instructions"])
        self.assertIn("Selected option: B: Orchestra pit", client.requests[0]["input"])

    def test_one_sentence_valid_fallback_and_known_limitations(self):
        d = question()
        self.assertIsNotNone(_validate("KEY: B\nSPEECH: Берём Афродита.", d))
        # The guard is deliberately not a full semantic fact-checker.
        # A single clause might still make a bad factual claim; manual QA
        # remains necessary before merging the feature branch.


if __name__ == "__main__":
    unittest.main()
