"""Offline tests: V5 stable delivery-frame variation and literal answer guard.

No LLM/DB calls. Test content-independent selection and unchanged text across
Russian, German, English, Hebrew, and multibyte option names.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from platinum_helper.core import choose_presentation_style
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

    def test_retry_keeps_style_and_selected_name(self):
        d = scenario(answer="Словения", presentation_style="CASUAL")
        client = FakeClient([
            "KEY: B\nSPEECH: Этот вариант мне ближе.",
            "KEY: B\nSPEECH: Ставлю на «Словения».",
        ])
        with patch.dict("os.environ", {"QUIZ_HELPER_MAX_ATTEMPTS": "2"}):
            result = talk(d, client=client)
        self.assertEqual(result, "Ставлю на «Словения».")
        self.assertEqual(len(client.requests), 2)
        for request in client.requests:
            self.assertIn("relaxed, conversational nudge", request["instructions"])
            self.assertIn("Selected option: B: Словения", request["input"])
            self.assertNotIn("Афина", request["input"])
        self.assertIn("NO factual justification", client.requests[1]["instructions"])

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
