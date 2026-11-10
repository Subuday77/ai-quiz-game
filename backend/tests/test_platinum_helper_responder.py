"""No paid API: regression tests for GPT-5.4 helper speech boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helper_fallback_fixtures import english_templates, russian_templates
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

    def test_dotted_initials_are_not_mistaken_for_extra_explanation(self):
        data = sample("KNOW")
        data.update(
            letter="A", delivery_style="TENTATIVE",
            question_text="Кто написал роман «Гарри Поттер и философский камень»?",
            choices={
                "A": "Дж. К. Роулинг", "B": "Дж. Р. Р. Толкин",
                "C": "Клайв С. Льюис", "D": "Роальд Даль",
            },
            correct_option_text="Дж. К. Роулинг",
        )
        speech = "Мне кажется, здесь «Дж. К. Роулинг»."
        self.assertEqual(_validate("KEY: A\nSPEECH: " + speech, data), speech)
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: " + speech + " Она написала книгу.", data
        ))
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: Советую «Дж. Р. Р. Толкин».", data
        ))
        for index in (0, 1):
            with self.subTest(template=index), patch(
                "platinum_helper.responder.random.SystemRandom"
            ) as rng:
                rng.return_value.choice.side_effect = lambda variants: variants[index]
                fallback = _fallback(data, russian_templates())
                self.assertIn("Дж. К. Роулинг", fallback)
                self.assertEqual(_validate("KEY: A\nSPEECH: " + fallback, data), fallback)
        sdk = FakeClient(["KEY: A\nSPEECH: " + speech])
        self.assertEqual(
            talk(data, client=sdk, fallback_loader=russian_templates), speech
        )
        self.assertEqual(sdk.calls, 1)

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

    def test_one_successful_model_request_keeps_frozen_key(self):
        client = FakeClient(["KEY: A\nSPEECH: Я выбираю 395."])
        with patch.dict("os.environ", {"QUIZ_HELPER_MODEL_ID": "gpt-5.4"}):
            self.assertEqual(talk(sample(), client=client, fallback_loader=russian_templates), "Я выбираю 395.")
        self.assertEqual(client.calls, 1)
        request = client.sent[0]
        self.assertEqual(request["model"], "gpt-5.4")
        self.assertEqual(request["reasoning"]["effort"], "low")
        self.assertIn("Selected option: A: 395", request["input"])
        self.assertNotIn("476", request["input"])

    def test_invalid_model_reply_uses_template_without_retry(self):
        client = FakeClient([
            "KEY: A\nSPEECH: 476 — правильный ответ.",
            "KEY: A\nSPEECH: Я выбираю 395.",
        ])
        with patch("platinum_helper.responder.random.SystemRandom") as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[0]
            self.assertEqual(talk(sample(), client=client, fallback_loader=russian_templates), "Я знаю: «395».")
        self.assertEqual(client.calls, 1)

    def test_debug_trace_distinguishes_model_fallback_and_unavailable(self):
        data = sample()
        data["call_id"] = "qa-call-id"

        with patch("platinum_helper.responder.response_source") as log:
            speech = talk(
                data, client=FakeClient(["KEY: A\nSPEECH: Я выбираю 395."]),
                fallback_loader=russian_templates,
            )
            self.assertEqual(speech, "Я выбираю 395.")
            self.assertEqual(log.call_args.kwargs["source"], "model")
            self.assertIsNone(log.call_args.kwargs.get("reason"))
            self.assertEqual(log.call_args.kwargs["call_id"], "qa-call-id")

        with patch("platinum_helper.responder.response_source") as log:
            speech = talk(
                data, client=FakeClient([RuntimeError("model not found")]),
                fallback_loader=russian_templates,
            )
            self.assertIn("395", speech)
            self.assertEqual(log.call_args.kwargs["source"], "fallback")
            self.assertEqual(log.call_args.kwargs["reason"], "provider_error")

        with patch("platinum_helper.responder.response_source") as log:
            speech = talk(
                data, client=FakeClient(["KEY: A\nSPEECH: Правильный ответ 476."]),
                fallback_loader=russian_templates,
            )
            self.assertIn("395", speech)
            self.assertEqual(log.call_args.kwargs["source"], "fallback")
            self.assertEqual(log.call_args.kwargs["reason"], "model_speech_rejected")

        with patch("platinum_helper.responder.response_source") as log:
            with self.assertRaises(HelperUnavailable):
                talk(
                    data, client=FakeClient([RuntimeError("model not found")]),
                    fallback_loader=lambda: {},
                )
            self.assertEqual(log.call_args.kwargs["source"], "unavailable")
            self.assertEqual(log.call_args.kwargs["reason"], "fallback_unavailable")

    def test_network_timeout_uses_template_without_retry(self):
        client = FakeClient([TimeoutError(), TimeoutError()])
        with patch("platinum_helper.responder.random.SystemRandom") as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[1]
            self.assertEqual(talk(sample(), client=client, fallback_loader=russian_templates), "Ну, здесь точно «395».")
        self.assertEqual(client.calls, 1)

    def test_incomplete_model_reply_uses_template_without_retry(self):
        client = FakeClient([("incomplete", ""), ("completed", "KEY: A\nSPEECH: 395.")])
        with patch("platinum_helper.responder.random.SystemRandom") as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[0]
            self.assertEqual(talk(sample(), client=client, fallback_loader=russian_templates), "Я знаю: «395».")
        self.assertEqual(client.calls, 1)

    def test_openai_initialization_error_uses_template(self):
        with patch("platinum_helper.responder.OpenAI",
                   side_effect=RuntimeError("OPENAI_API_KEY not set")) as constructor, patch(
            "platinum_helper.responder.random.SystemRandom"
        ) as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[0]
            self.assertEqual(talk(sample(), fallback_loader=russian_templates), "Я знаю: «395».")
        self.assertEqual(constructor.call_count, 1)

    def test_unsure_outage_never_mentions_any_choice(self):
        client = FakeClient([TimeoutError()])
        reply = talk(sample("UNSURE"), client=client, fallback_loader=russian_templates)
        self.assertEqual(reply, "Тут я помочь не могу.")
        self.assertEqual(client.calls, 1)
        for option in sample("UNSURE")["choices"].values():
            self.assertNotIn(option, reply)

    def test_randomized_templates_do_not_reveal_correctness(self):
        # KNOW vs MISTAKEN must use the same two templates for a given
        # confidence. Their selected keys differ, not their wording.
        for style in ("CONFIDENT", "TENTATIVE"):
            for index in (0, 1):
                with self.subTest(style=style, index=index), patch(
                    "platinum_helper.responder.random.SystemRandom"
                ) as rng:
                    rng.return_value.choice.side_effect = lambda variants: variants[index]
                    know, mistake = sample("KNOW"), sample("MISTAKEN")
                    know["delivery_style"] = mistake["delivery_style"] = style
                    yes, no = _fallback(know, russian_templates()), _fallback(mistake, russian_templates())
                    self.assertEqual(yes.replace("476", "{answer}"),
                                     no.replace("395", "{answer}"))
                    self.assertIn("476", yes)
                    self.assertIn("395", no)
                    self.assertNotIn("476", no)
                    self.assertNotIn("395", yes)

    def test_missing_cached_localization_cannot_show_unsafe_fallback(self):
        data = sample()
        with self.assertRaises(HelperUnavailable):
            _fallback(data, {})

    def test_safe_fallback_covers_all_supported_languages(self):
        for language in ("en", "de", "fr", "es", "it", "pt", "ru",
                         "he", "ar", "ja", "zh"):
            for style in ("CONFIDENT", "TENTATIVE"):
                data = sample()
                data["speech_language"] = language
                data["delivery_style"] = style
                with self.subTest(language=language, style=style):
                    for index in (0, 1):
                        # This loop tests the key/placeholder contract, not
                        # AI Localizer fluency. The latter is covered by cache
                        # integration and real-language validator tests.
                        with patch("platinum_helper.responder.random.SystemRandom") as rng, patch(
                            "platinum_helper.responder._validate",
                            side_effect=lambda raw, _: raw.split("SPEECH: ", 1)[1],
                        ):
                            rng.return_value.choice.side_effect = lambda variants: variants[index]
                            response = _fallback(data, english_templates())
                        self.assertIn("395", response)
                        for other in ("410", "476", "527"):
                            self.assertNotIn(other, response)
                        self.assertTrue(response.strip())
            data["outcome"] = "UNSURE"
            with patch("platinum_helper.responder._validate",
                       side_effect=lambda raw, _: raw.split("SPEECH: ", 1)[1]):
                self.assertTrue(_fallback(data, english_templates()))
                for option in data["choices"].values():
                    self.assertNotIn(option, _fallback(data, english_templates()))


if __name__ == "__main__":
    unittest.main()
