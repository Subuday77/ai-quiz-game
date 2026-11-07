"""Offline per-hire speech-memory regressions. No model or external DB."""
import unittest

from platinum_helper.responder import _prompt, _validate
from platinum_helper.speech_history import HISTORY_LIMIT, MASK, anonymize_reply
from test_platinum_helper_speech_frames import scenario


class SpeechHistoryPromptTests(unittest.TestCase):
    def test_lookback_two_and_original_speech_untouched(self):
        self.assertEqual(HISTORY_LIMIT, 2)
        choices = {
            "A": "Лев Толстой", "B": "Фёдор Достоевский",
            "C": "Иван Тургенев", "D": "Антон Чехов",
        }
        first = "Мне кажется, тут Лев Толстой."
        second = "По-моему, тут Ионический."
        first_masked = anonymize_reply(first, choices, "A")
        second_masked = anonymize_reply(second, {
            "A": "Дорический", "B": "Ионический",
            "C": "Коринфский", "D": "Тосканский",
        }, "B")
        self.assertEqual(first_masked, "Мне кажется, тут [ANSWER].")
        self.assertEqual(second_masked, "По-моему, тут [ANSWER].")
        self.assertEqual(first, "Мне кажется, тут Лев Толстой.")
        self.assertNotIn("Лев Толстой", first_masked)
        self.assertNotIn("Ионический", second_masked)
        d = scenario(answer="Рулевая рейка", speech_language="ru",
                     presentation_style="CASUAL", confidence="TENTATIVE")
        d["speech_history"] = [first_masked, second_masked]
        instructions, prompt = _prompt(d)
        self.assertIn("ONLY in Russian", instructions)
        self.assertIn("Selected option: B: Рулевая рейка", prompt)
        self.assertIn("Мне кажется, тут [ANSWER].", prompt)
        self.assertIn("По-моему, тут [ANSWER].", prompt)
        self.assertIn("Vary your sentence opening, filler words", prompt)
        self.assertNotIn("Лев Толстой", prompt)
        self.assertNotIn("Ионический", prompt)
        self.assertNotIn("correct", prompt.casefold().split("Previously spoken")[0])
        self.assertEqual(
            _validate("KEY: B\nSPEECH: Скорее, Рулевая рейка.", d),
            "Скорее, Рулевая рейка.",
        )

    def test_mask_all_choices_even_if_unexpectedly_mentioned(self):
        choices = {"A": "Paris", "B": "Lyon", "C": "Berlin", "D": "Rome"}
        self.assertEqual(
            anonymize_reply("Paris или Lyon? Пожалуй, Lyon.", choices, "B"),
            "[ANSWER] или [ANSWER]? Пожалуй, [ANSWER].",
        )
        self.assertIsNone(anonymize_reply("Моё мнение.", choices, "B"))
        self.assertIsNone(anonymize_reply("", choices, "B"))
        self.assertIsNone(anonymize_reply("Пример.", {"A": "Paris"}, "B"))
        self.assertIsNone(anonymize_reply("Тут Paris.", choices, "X"))
        self.assertEqual(
            anonymize_reply("Не знаю, тут пас.", choices, None),
            "Не знаю, тут пас.",
        )

    def test_case_fold_and_mixed_scripts_do_not_leak_old_name(self):
        names = {"A": "André-Marie Ampère", "B": "Ottawa",
                 "C": "Тобайас Смоллетт", "D": "Paris"}
        self.assertEqual(
            anonymize_reply("Я за ANDRÉ-MARIE AMPÈRE.", names, "A"),
            "Я за [ANSWER].",
        )
        self.assertEqual(
            anonymize_reply("ここは Ottawa でいこう。", names, "B"),
            "ここは [ANSWER] でいこう。",
        )
        self.assertEqual(
            anonymize_reply("أميل إلى Тобайас Смоллетт.", names, "C"),
            "أميل إلى [ANSWER].",
        )
        # NFKC-comparable but non-literal spelling: fail closed.
        self.assertIsNone(anonymize_reply(
            "ここは Ｏｔｔａｗａ でいこう。", names, "B",
        ))

    def test_history_not_sent_when_empty_and_no_correctness_exposed(self):
        d = scenario(answer="Словения", speech_language="ru")
        plain = _prompt(d)
        self.assertEqual(plain, _prompt({**d, "speech_history": []}))
        d["speech_history"] = ["По-моему, [ANSWER]."]
        for outcome in ("KNOW", "MISTAKEN"):
            inst, prompt = _prompt({**d, "outcome": outcome}, strict=True)
            self.assertIn("По-моему, [ANSWER].", prompt)
            self.assertIn("strict" if False else "Your earlier response", inst)
            self.assertNotIn("MISTAKEN", inst)
            self.assertNotIn("KNOW", inst)
        know = _prompt({**d, "outcome": "KNOW"})
        mistaken = _prompt({**d, "outcome": "MISTAKEN"})
        self.assertEqual(know, mistaken)
        unsure = _prompt({**d, "outcome": "UNSURE", "letter": None})
        self.assertIn("По-моему, [ANSWER].", unsure[1])
        self.assertNotIn("Словения", unsure[1])


if __name__ == "__main__":
    unittest.main()
