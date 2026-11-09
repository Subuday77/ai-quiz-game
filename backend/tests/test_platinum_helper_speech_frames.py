"""Offline tests: V5 stable delivery-frame variation and literal answer guard.

No LLM/DB calls. Test content-independent selection and unchanged text across
Russian, German, English, Hebrew, and multibyte option names.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from platinum_helper.core import choose_presentation_style
from helper_fallback_fixtures import russian_templates
from platinum_helper.responder import _prompt, _validate, talk


def scenario(answer="Афродита", language="Russian", speech_language="ru",
             outcome="MISTAKEN", presentation_style="DIRECT", confidence="CONFIDENT"):
    return {
        "name": "Светозар", "profession": "Physicist", "gender": "male",
        "nationality": "Russia", "language": language, "speech_language": speech_language,
        "outcome": outcome, "letter": "B",
        "delivery_style": confidence, "presentation_style": presentation_style,
        "question_text": "Welcher Name ist richtig?",
        "choices": {"A": "Афина", "B": answer, "C": "Гера", "D": "Артемида"},
    }


class FakeClient:
    def __init__(self, drafts):
        self.drafts = iter(drafts)
        self.requests = []
        self.responses = self

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(status="completed", output_text=next(self.drafts))


class SpeechFrameTests(unittest.TestCase):
    def test_style_has_three_frames_and_is_frozen_for_same_id(self):
        ids = [UUID(int=i) for i in range(300)]
        styles = [choose_presentation_style(uid) for uid in ids]
        self.assertEqual(set(styles), {"DIRECT", "PERSONAL", "CASUAL"})
        for uid, style in zip(ids, styles):
            for _ in range(3):
                self.assertEqual(choose_presentation_style(uid), style)

    def test_style_changes_prompt_without_disclosing_correctness(self):
        modes = {}
        for style in ("DIRECT", "PERSONAL", "CASUAL"):
            d = scenario(presentation_style=style)
            wrong, wrong_prompt = _prompt(d)
            correct, correct_prompt = _prompt({**d, "outcome": "KNOW"})
            self.assertEqual(wrong, correct)
            self.assertEqual(wrong_prompt, correct_prompt)
            self.assertNotIn("MISTAKEN", wrong)
            self.assertNotIn("KNOW", wrong)
            self.assertNotIn("Афина", wrong_prompt)
            self.assertIn("Selected option: B: Афродита", wrong_prompt)
            self.assertIn("ONLY in Russian", wrong)
            self.assertIn("Do NOT add facts", wrong)
            modes[style] = wrong
        self.assertEqual(len(set(modes.values())), 3)

    def test_confidence_and_style_are_distinct(self):
        for style in ("DIRECT", "PERSONAL", "CASUAL"):
            for confidence in ("CONFIDENT", "TENTATIVE"):
                d = scenario(presentation_style=style, confidence=confidence)
                instruction, _ = _prompt(d)
                self.assertIn("Speak directly and confidently" if confidence == "CONFIDENT"
                              else "Sound mildly uncertain", instruction)
                self.assertIn("ONLY in Russian", instruction)
                self.assertEqual(
                    _prompt(d)[0], _prompt({**d, "outcome": "KNOW"})[0]
                )

    def test_selected_name_required_not_just_key_or_description(self):
        d = scenario()
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Да, мне этот ответ нравится.", d
        ))
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Я за вариант B.", d
        ))
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Думаю, Афина.", d
        ))
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: Мой выбор: «Афродита».", d
        ), "Мой выбор: «Афродита».")

    def test_cross_language_names_and_unicode_are_accepted_unchanged(self):
        items = (
            ("ru", "German", "André-Marie Ampère",
             "Я за «André-Marie Ampère»."),
            ("de", "Russian", "Афродита",
             "Mein Tipp ist „Афродита“."),
            ("en", "German", "Quecksilber",
             "I'd pick Quecksilber."),
            ("ru", "German", "Trimurti",
             "Пожалуй, Trimurti."),
            ("he", "German", "Quecksilber",
             "אני בעד Quecksilber."),
            ("en", "French", "André-Marie Ampère",
             "I'd pick André-Marie Ampère."),
        )
        for ui, question_language, answer, speech in items:
            with self.subTest(ui=ui, answer=answer):
                d = scenario(answer=answer, language=question_language,
                             speech_language=ui)
                self.assertEqual(
                    _validate("KEY: B\nSPEECH: " + speech, d), speech
                )

    def test_partial_or_inflected_answer_does_not_pass(self):
        d = scenario(answer="André-Marie Ampère")
        for speech in (
            "Мне нравится Ampère.",
            "Я за André-Marie.",
            "Я бы выбрал Андре-Мари Ампера.",
            "Ответ — другой.",
        ):
            with self.subTest(speech=speech):
                self.assertIsNone(_validate("KEY: B\nSPEECH: " + speech, d))
        # NFKC/casefold: Unicode and capitalization differences are okay.
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: По-моему, ANDRÉ-MARIE AMPÈRE.", d
        ), "По-моему, ANDRÉ-MARIE AMPÈRE.")

    def test_em_dash_is_rejected_except_inside_verbatim_answer_name(self):
        plain = scenario(answer="Quecksilber", language="German", speech_language="ru")
        instruction, _ = _prompt(plain)
        self.assertIn("Unicode em dash (U+2014)", instruction)
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Выбирай Quecksilber — мой совет.", plain
        ))
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Quecksilber — хорошая ставка.", plain
        ))
        # Other punctuation remains valid; we ban ONLY U+2014, not hyphens
        # in names or incidental commas / colons.
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: Мой совет: «Quecksilber».", plain
        ), "Мой совет: «Quecksilber».")

        with_dash = scenario(answer="Paris — Lyon", speech_language="ru")
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: Пожалуй, «Paris — Lyon».", with_dash
        ), "Пожалуй, «Paris — Lyon».")
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Paris — Lyon — мой выбор.", with_dash
        ))
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Я за Paris — Lyon — точно.", with_dash
        ))
        unsure = scenario(outcome="UNSURE")
        self.assertIsNone(_validate(
            "SPEECH: Тут — пас.", unsure
        ))
        self.assertEqual(_validate(
            "SPEECH: Тут, пожалуй, пас.", unsure
        ), "Тут, пожалуй, пас.")

    def test_em_dash_rejected_then_single_fallback_with_same_frame(self):
        d = scenario(answer="Словения", presentation_style="DIRECT")
        client = FakeClient([
            "KEY: B\nSPEECH: Словения — мой выбор.",
            "KEY: B\nSPEECH: Я за «Словения».",
        ])
        with patch("platinum_helper.responder.random.SystemRandom") as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[0]
            self.assertEqual(talk(d, client=client, fallback_loader=russian_templates), "Я знаю: «Словения».")
        self.assertEqual(len(client.requests), 1)
        self.assertIn("Delivery approach: A direct", client.requests[0]["instructions"])
        self.assertIn("Unicode em dash (U+2014)", client.requests[0]["instructions"])

    def test_fallback_keeps_style_and_selected_name(self):
        d = scenario(answer="Словения", presentation_style="CASUAL")
        client = FakeClient([
            "KEY: B\nSPEECH: Этот вариант мне ближе.",
            "KEY: B\nSPEECH: Ставлю на «Словения».",
        ])
        with patch("platinum_helper.responder.random.SystemRandom") as rng:
            rng.return_value.choice.side_effect = lambda variants: variants[1]
            result = talk(d, client=client, fallback_loader=russian_templates)
        self.assertEqual(result, "Ну, здесь точно «Словения».")
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertIn("A short, relaxed, off-the-cuff opinion", request["instructions"])
        self.assertIn("Selected option: B: Словения", request["input"])
        self.assertNotIn("Афина", request["input"])

    def test_casual_tentative_is_not_a_hedged_command_or_vocative(self):
        # The V6 30-case live test exposed awkward RU/AR tentative
        # "I think + take" instructions and a JA vocative ("friend").
        # Test the *model instruction*, not invented language-specific
        # rejection regexes that could break perfectly valid sentences.
        examples = (
            ("ru", "Russian", "Airplane", "По-моему, Airplane."),
            ("ar", "Arabic", "Тобайас Смоллетт",
             "أميل إلى Тобайас Смоллетт."),
            ("ja", "Japanese", "Ottawa", "Ottawaかな。"),
        )
        for ui, language, answer, good_speech in examples:
            for confidence in ("CONFIDENT", "TENTATIVE"):
                data = scenario(
                    answer=answer, language="German", speech_language=ui,
                    confidence=confidence, presentation_style="CASUAL",
                )
                wrong_instruction, wrong_payload = _prompt(data)
                right_instruction, right_payload = _prompt(
                    {**data, "outcome": "KNOW"}
                )
                with self.subTest(ui=ui, confidence=confidence):
                    self.assertEqual(wrong_instruction, right_instruction)
                    self.assertEqual(wrong_payload, right_payload)
                    self.assertIn(f"ONLY in {language}", wrong_instruction)
                    self.assertIn("Avoid commands, imperatives", wrong_instruction)
                    self.assertIn("second-person directives", wrong_instruction)
                    self.assertIn("hedge the opinion itself", wrong_instruction)
                    self.assertIn("equivalent vocative", wrong_instruction)
                    self.assertNotIn("using natural second-person", wrong_instruction)
                    self.assertIn(
                        "Speak directly and confidently" if confidence == "CONFIDENT"
                        else "Sound mildly uncertain", wrong_instruction
                    )
                    self.assertIn(f"Selected option: B: {answer}", wrong_payload)
                self.assertEqual(
                    _validate("KEY: B\nSPEECH: " + good_speech, data),
                    good_speech,
                )
        # Unrelated speech approaches and UNSURE behavior remain unchanged.
        plain, _ = _prompt(scenario(presentation_style="DIRECT"))
        personal, _ = _prompt(scenario(presentation_style="PERSONAL"))
        unsure, _ = _prompt(scenario(presentation_style="CASUAL", outcome="UNSURE"))
        for prompt in (plain, personal, unsure):
            self.assertNotIn("equivalent vocative", prompt)

    def test_unsure_uses_style_without_revealing_answer(self):
        styles = set()
        for style in ("DIRECT", "PERSONAL", "CASUAL"):
            d = scenario(outcome="UNSURE", presentation_style=style)
            instr, payload = _prompt(d)
            self.assertNotIn("Афродита", payload)
            self.assertIn("ONLY in Russian", instr)
            self.assertNotIn("Selected option", payload)
            self.assertNotIn("Афина", payload)
            styles.add(instr)
        self.assertEqual(len(styles), 3)

    def test_invalid_style_fails_closed(self):
        d = scenario(presentation_style="INVENTED")
        with self.assertRaisesRegex(ValueError, "presentation style"):
            _prompt(d)


if __name__ == "__main__":
    unittest.main()
