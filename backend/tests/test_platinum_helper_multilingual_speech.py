"""Offline multilingual grammar-safe speech tests: no morphology engine.

The model is instructed to use the selected answer as an unchanged option
label, not inflect it to fit an accusative or any other grammatical case.
These tests cover contracts and accepted speech, not actual LLM fluency.
"""
import unittest

from platinum_helper.responder import _fallback, _prompt, _validate


def scenario(*, language="Russian", answer="Словения", outcome="MISTAKEN",
             style="CONFIDENT"):
    return {
        "name": "Alex", "profession": "Friend", "gender": "female",
        "nationality": "unspecified", "language": language,
        "outcome": outcome, "delivery_style": style,
        "letter": "B", "question_text": "Which of these?",
        "choices": {"A": "Other", "B": answer,
                    "C": "Different", "D": "Unrelated"},
    }


class MultilingualSpeechTests(unittest.TestCase):
    def test_know_and_mistaken_have_identical_instructions_for_each_language(self):
        languages = (
            "Russian", "English", "Hebrew", "German", "French",
            "Spanish", "Italian", "Portuguese", "Arabic",
            "Japanese", "Chinese",
        )
        for language in languages:
            with self.subTest(language=language):
                for style in ("CONFIDENT", "TENTATIVE"):
                    data = scenario(language=language, style=style)
                    know = dict(data, outcome="KNOW")
                    mistaken = dict(data, outcome="MISTAKEN")
                    i1, p1 = _prompt(know)
                    i2, p2 = _prompt(mistaken)
                    self.assertEqual(i1, i2)
                    self.assertEqual(p1, p2)
                    self.assertIn("Include the chosen text exactly as printed", i1)
                    self.assertIn("with or without noun cases", i1)
                    self.assertIn(f"ONLY in {language}", i1)
                    self.assertIn("Do NOT add facts", i1)
                    self.assertNotIn("MISTAKEN", i1)
                    self.assertNotIn("KNOW", i1)

    def test_no_hardcoded_declension_or_inflection_changes_in_fallback(self):
        # Check both approved variants for each confidence and every unusual
        # nominative form: insert the exact original option only once.
        from unittest.mock import patch
        names = ("Афродита", "Антон Чехов", "Словения", "Литва", "Brassica napus")
        for name in names:
            for style in ("CONFIDENT", "TENTATIVE"):
                for index in (0, 1):
                    with self.subTest(name=name, style=style, index=index), patch(
                        "platinum_helper.responder.random.SystemRandom"
                    ) as rng:
                        rng.return_value.choice.side_effect = lambda variants: variants[index]
                        data = scenario(answer=name, style=style)
                        speech = _fallback(data)
                        self.assertEqual(speech.count(name), 1)
                        self.assertEqual(
                            _validate(f"KEY: B\nSPEECH: {speech}", data), speech
                        )

    def test_same_answer_is_embeddable_in_idiomatic_languages(self):
        examples = (
            ("Russian", "Афродита", "Мой ответ: «Афродита»."),
            ("English", "Aphrodite", "I'd go with the option \"Aphrodite\"."),
            ("German", "Aphrodite", "Mein Tipp ist die Option „Aphrodite“."),
            ("French", "Aphrodite", "Je pense à l’option «Aphrodite»."),
            ("Spanish", "Afrodita", "Mi respuesta es «Afrodita»."),
            ("Italian", "Afrodite", "La mia risposta è «Afrodite»."),
            ("Hebrew", "סלובניה", "הייתי בוחר באפשרות «סלובניה»."),
        )
        for language, answer, speech in examples:
            with self.subTest(language=language):
                data = scenario(language=language, answer=answer)
                self.assertEqual(
                    _validate(f"KEY: B\nSPEECH: {speech}", data), speech
                )

    def test_unsure_remains_answer_free_and_allows_conversational_variation(self):
        data = scenario(outcome="UNSURE")
        prompt, user = _prompt(data)
        self.assertIn("using your own casual, human wording", prompt)
        self.assertNotIn("Словения", user)
        for speech in ("Не помню, подсказать не смогу.", "Тут, пожалуй, пас."):
            with self.subTest(speech=speech):
                self.assertEqual(_validate("SPEECH: " + speech, data), speech)
        self.assertIsNone(_validate("SPEECH: Думаю, Словения.", data))

    def test_bad_explanatory_claims_still_fail(self):
        data = scenario(answer="Словения")
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Я за Словения; она граничит с Италией.", data
        ))
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: Мой ответ — «Словения». "
            "У неё выход к Адриатике.", data
        ))


if __name__ == "__main__":
    unittest.main()
