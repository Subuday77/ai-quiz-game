"""The helper's SPOKEN language is the UI locale, not GameRun.language.

No paid provider calls or browser. The server still owns answer selection.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException, Request

from persistence.models import User
from platinum_helper.identity import UI_FAKER_LOCALES
from platinum_helper.responder import _fallback, _prompt, _validate
from platinum_helper.routes import CallInput, call_helper


def german_question(*, speech_language="ru", outcome="KNOW"):
    return {
        "name": "Светозар Матвеев", "profession": "Physicist",
        "language": "German", "speech_language": speech_language,
        "question_text": "Welches chemische Element ist bei Raumtemperatur flüssig?",
        "choices": {
            "A": "Quecksilber", "B": "Aluminium",
            "C": "Schwefel", "D": "Kohlenstoff",
        },
        "letter": "A" if outcome == "KNOW" else None,
        "outcome": outcome, "delivery_style": "TENTATIVE",
        "gender": "male", "nationality": "Russia",
    }


class UiSpeechTests(unittest.TestCase):
    def test_german_question_and_russian_interface_russian_reply(self):
        question = german_question()
        instructions, prompt = _prompt(question)
        self.assertIn("ONLY in Russian", instructions)
        self.assertNotIn("Answer ONLY in German", instructions)
        self.assertIn("Welches chemische Element", prompt)
        self.assertIn("Selected option: A: Quecksilber", prompt)
        self.assertNotIn("KNOW", instructions)
        speech = "Мне кажется, ответ: «Quecksilber»."
        self.assertEqual(
            _validate(f"KEY: A\nSPEECH: {speech}", question), speech
        )
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: Ich würde eher Quecksilber nehmen.", question
        ))
        self.assertIn("Quecksilber", _fallback(question))
        self.assertNotIn("—", _fallback(question))

    def test_unsure_uses_ui_language_even_without_a_selected_option(self):
        question = german_question(outcome="UNSURE")
        instructions, _ = _prompt(question)
        self.assertIn("ONLY in Russian", instructions)
        self.assertEqual(
            _validate("SPEECH: Тут я, пожалуй, не подскажу.", question),
            "Тут я, пожалуй, не подскажу.",
        )
        self.assertIsNone(_validate(
            "SPEECH: Da kann ich dir leider keinen Rat geben.", question
        ))
        self.assertNotIn("Quecksilber", _fallback(question))

    def test_russian_question_and_german_ui_keep_russian_option_unchanged(self):
        question = german_question(speech_language="de")
        question["language"] = "Russian"
        question["choices"]["A"] = "Афродита"
        instructions, _ = _prompt(question)
        self.assertIn("ONLY in German", instructions)
        speech = "Mein Tipp ist „Афродита“."
        self.assertEqual(_validate(f"KEY: A\nSPEECH: {speech}", question), speech)
        self.assertIsNone(_validate(
            "KEY: A\nSPEECH: Наверное, Афродита.", question
        ))

    def test_all_supported_ui_locales_are_independent_of_question_language(self):
        names = {
            "en": "English", "de": "German", "fr": "French",
            "es": "Spanish", "it": "Italian", "pt": "Portuguese",
            "ru": "Russian", "he": "Hebrew", "ar": "Arabic",
            "ja": "Japanese", "zh": "Chinese",
        }
        self.assertEqual(set(names), set(UI_FAKER_LOCALES))
        for code, name in names.items():
            with self.subTest(ui=code):
                question = german_question(speech_language=code)
                instructions, _ = _prompt(question)
                self.assertIn(f"ONLY in {name}", instructions)
                self.assertIn("Quecksilber", _fallback(question))
                know = dict(question, outcome="MISTAKEN")
                self.assertEqual(_prompt(question)[0], _prompt(know)[0])

    def test_route_passes_current_ui_locale_and_keeps_game_language(self):
        user = User(
            id=UUID(int=1), username="tester", username_key="tester",
            ui_language="ru",
        )
        d = german_question()
        d.pop("speech_language")
        d["call_id"] = UUID(int=3)
        request = Request({"type": "http", "headers": []})
        run_id = UUID(int=4)
        body = CallInput(question_id=UUID(int=5), request_key="request-12345",
                         ui_language="ru")
        with patch("platinum_helper.routes.validate_origin"), \
             patch("platinum_helper.routes.reserve_call",
                   return_value=(None, {"consultations_remaining": 2}, False, d)), \
             patch("platinum_helper.routes.talk",
                   return_value="Мне кажется, «Quecksilber».") as speaker, \
             patch("platinum_helper.routes.finish_call",
                   return_value={"text": "Мне кажется, «Quecksilber».",
                                 "helper": {"consultations_remaining": 2}}):
            result = call_helper(run_id, body, request, user=user, db=object())
        self.assertIn("Quecksilber", result["text"])
        actual = speaker.call_args.args[0]
        self.assertEqual(actual["language"], "German")
        self.assertEqual(actual["speech_language"], "ru")
        self.assertEqual(actual["choices"]["A"], "Quecksilber")
        self.assertEqual(actual["letter"], "A")

    def test_older_frontend_falls_back_to_persisted_ui_preference(self):
        user = User(
            id=UUID(int=1), username="tester", username_key="tester",
            ui_language="ru",
        )
        request = Request({"type": "http", "headers": []})
        body = CallInput(question_id=UUID(int=5), request_key="request-12345")
        data = german_question()
        data.pop("speech_language")
        data["call_id"] = UUID(int=3)
        with patch("platinum_helper.routes.validate_origin"), \
             patch("platinum_helper.routes.reserve_call",
                   return_value=(None, {}, False, data)), \
             patch("platinum_helper.routes.talk", return_value="Тут пас.") as speaker, \
             patch("platinum_helper.routes.finish_call",
                   return_value={"text": "Тут пас.", "helper": {}}):
            call_helper(UUID(int=4), body, request, user=user, db=object())
        self.assertEqual(speaker.call_args.args[0]["speech_language"], "ru")

    def test_unsupported_language_rejected_before_reserving_paid_call(self):
        user = User(
            id=UUID(int=1), username="tester", username_key="tester",
            ui_language="ru",
        )
        request = Request({"type": "http", "headers": []})
        body = CallInput(question_id=UUID(int=5), request_key="request-12345",
                         ui_language="xx")
        with patch("platinum_helper.routes.validate_origin"), \
             patch("platinum_helper.routes.reserve_call") as reserve:
            with self.assertRaises(HTTPException) as caught:
                call_helper(UUID(int=4), body, request, user=user, db=object())
        self.assertEqual(caught.exception.status_code, 422)
        reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
