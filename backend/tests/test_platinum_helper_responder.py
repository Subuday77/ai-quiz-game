"""No paid API: regression tests for GPT-5.4 helper speech boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from platinum_helper.responder import (
    HelperUnavailable, _fallback, _prompt, _validate, talk,
)


def sample(outcome="MISTAKEN"):
    return dict(
        name="Sam", profession="Historian", language="Russian",
        topic="History", outcome=outcome, delivery_style="CONFIDENT",
        letter="A" if outcome == "MISTAKEN" else "C",
        question_text="В каком году?",
        choices={"A": "395", "B": "410", "C": "476", "D": "527"},
        correct_option_text="476",
    )


class FakeClient:
    def __init__(self, items):
        self.items = iter(items)
        self.calls = 0
        self.sent = []
        self.responses = self

    def with_options(self, **kwargs):
        assert kwargs == {"timeout": 8.0, "max_retries": 0}
        return self

    def create(self, **kwargs):
        self.calls += 1
        self.sent.append(kwargs)
        value = next(self.items)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            status, raw = value
        else:
            status, raw = "completed", value
        return SimpleNamespace(status=status, output_text=raw)


class HelperResponderTests(unittest.TestCase):
    def test_same_model_instructions_for_know_and_mistaken(self):
        know = sample("KNOW")
        wrong = sample("MISTAKEN")
        inst_know, prompt_know = _prompt(know)
        inst_wrong, prompt_wrong = _prompt(wrong)
        self.assertEqual(inst_know, inst_wrong)
        self.assertNotEqual(prompt_know, prompt_wrong)
        self.assertNotIn("476", prompt_wrong)
        self.assertNotIn("MISTAKEN", inst_wrong)
        self.assertIn("Selected option:", prompt_wrong)
        for style in ("CONFIDENT", "TENTATIVE"):
            know["delivery_style"] = wrong["delivery_style"] = style
            self.assertEqual(_prompt(know)[0], _prompt(wrong)[0])

    def test_unsure_knows_question_but_not_choices(self):
        data = sample("UNSURE")
        inst, prompt = _prompt(data)
        self.assertIn("В каком году?", prompt)
        self.assertNotIn("476", prompt)
        self.assertNotIn("410", prompt)
        self.assertIn("ALREADY looking", inst)
        self.assertEqual(_validate("SPEECH: Я не уверен.", data), "Я не уверен.")
        self.assertIsNone(_validate("SPEECH: Пришли вопрос и варианты.", data))
        self.assertIsNone(_validate("SPEECH: Я думаю, 476.", data))

    def test_literal_leak_including_cassini_huygens(self):
        data = sample()
        self.assertIsNone(_validate("KEY: A\nSPEECH: Впрочем, 476.", data))
        self.assertIsNone(_validate("KEY: C\nSPEECH: Наверное, 395.", data))
        titan = sample()
        titan.update(
            letter="B",
            question_text="Кто первым сел на Титан?",
            choices={"A": "Гюйгенс", "B": "Кассини", "C": "Вояджер", "D": "Маринер"},
            correct_option_text="Гюйгенс",
        )
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Советую Кассини: миссия Кассини—Гюйгенс изучала Титан.", titan
        ))
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: Я бы взял Кассини.", titan
        ), "Я бы взял Кассини.")

    def test_self_negation_and_no_false_numeric_substrings(self):
        data = sample()
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: Я бы не советовал выбирать 395.", data
        ))
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: 395 — это неправильный ответ.", data
        ))
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: Я бы выбрал вариант C.", data
        ))
        small = sample()
        small.update(letter="B", choices={"A": "4", "B": "451", "C": "5", "D": "6"})
        self.assertEqual(
            _validate("KEY: B\nSPEECH: Я выбираю 451.", small), "Я выбираю 451."
        )

    def test_bounded_retry_does_not_change_frozen_key(self):
        client = FakeClient([
            "KEY: A\nSPEECH: 476 — правильный ответ.",
            "KEY: A\nSPEECH: Я бы выбрал 395.",
        ])
        with patch.dict("os.environ", {"QUIZ_HELPER_MODEL_ID": "gpt-5.4"}):
            self.assertEqual(talk(sample(), client=client), "Я бы выбрал 395.")
        self.assertEqual(client.calls, 2)
        for request in client.sent:
            self.assertEqual(request["model"], "gpt-5.4")
            self.assertEqual(request["reasoning"]["effort"], "low")
            self.assertIn("Selected option: A: 395", request["input"])
            self.assertNotIn("476", request["input"])
        self.assertIn("NO factual justification", client.sent[1]["instructions"])

    def test_two_rejected_drafts_get_deterministic_safe_fallback(self):
        client = FakeClient([
            "KEY: A\nSPEECH: 476.",
            "KEY: A\nSPEECH: Я бы не советовал выбирать 395.",
        ])
        self.assertEqual(talk(sample(), client=client), "Мой ответ: «395».")
        self.assertEqual(client.calls, 2)
        self.assertEqual(_fallback(sample("UNSURE")), "Не уверен, не хочу тебя запутать.")

    def test_network_failures_are_not_charged(self):
        client = FakeClient([TimeoutError(), TimeoutError()])
        with self.assertRaises(HelperUnavailable):
            talk(sample(), client=client)
        self.assertEqual(client.calls, 2)

    def test_mixed_timeout_then_valid(self):
        client = FakeClient([TimeoutError(), "KEY: A\nSPEECH: Я выбрал 395."])
        self.assertEqual(talk(sample(), client=client), "Я выбрал 395.")
        self.assertEqual(client.calls, 2)

    def test_openai_initialization_error_is_unavailable(self):
        with patch("platinum_helper.responder.OpenAI",
                   side_effect=RuntimeError("OPENAI_API_KEY not set")) as constructor:
            with self.assertRaises(HelperUnavailable):
                talk(sample())
        self.assertEqual(constructor.call_count, 2)

    def test_safe_fallback_covers_all_supported_languages(self):
        for language in ("en", "de", "fr", "es", "it", "pt", "ru",
                         "he", "ar", "ja", "zh"):
            with self.subTest(language=language):
                data = sample()
                data["language"] = language
                self.assertIn("395", _fallback(data))
                data["delivery_style"] = "TENTATIVE"
                self.assertIn("395", _fallback(data))
                data["outcome"] = "UNSURE"
                self.assertTrue(_fallback(data))
                self.assertNotIn("395", _fallback(data))

    def test_two_incomplete_responses_are_not_charged(self):
        client = FakeClient([("incomplete", ""), ("incomplete", "")])
        with self.assertRaises(HelperUnavailable):
            talk(sample(), client=client)


if __name__ == "__main__":
    unittest.main()
