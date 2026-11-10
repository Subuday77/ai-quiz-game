"""Full-label validator boundaries for natural no-space Japanese/Chinese.

No paid calls. Prevent mixed-script false negatives without accepting
numeric prefixes, Latin word substrings or additional em-dash punctuation.
"""
import unittest

from platinum_helper.responder import _mentions, _validate


def scenario(answer, ui):
    return {
        "language": "German", "speech_language": ui,
        "outcome": "KNOW", "letter": "B",
        "choices": {
            "A": "Other A", "B": answer, "C": "Other C", "D": "Other D",
        },
    }


class CjkAnswerBoundariesTests(unittest.TestCase):
    def test_latin_answer_followed_by_japanese_particle(self):
        data = scenario("André-Marie Ampère", "ja")
        sentence = "André-Marie Ampèreでいこう。"
        self.assertTrue(_mentions(sentence, data["choices"]["B"]))
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: " + sentence, data
        ), sentence)

    def test_latin_answer_in_chinese_unspaced_sentence(self):
        data = scenario("Quecksilber", "zh")
        sentence = "我选Quecksilber。"
        self.assertTrue(_mentions(sentence, data["choices"]["B"]))
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: " + sentence, data
        ), sentence)

    def test_han_answer_followed_by_kana_particle(self):
        data = scenario("東京", "ja")
        sentence = "東京を選んで。"
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: " + sentence, data
        ), sentence)

    def test_no_false_numeric_or_subword_matches(self):
        for speech, answer in (
            ("451年を選んで。", "4"),
            ("Parisianでいこう。", "Paris"),
            ("東京都を選んで。", "東京"),
            ("Мировойを選んで。", "Мир"),
            ("Correct-German-30でいこう。", "Correct-German-3"),
        ):
            with self.subTest(speech=speech, answer=answer):
                self.assertFalse(_mentions(speech, answer))
                self.assertIsNone(_validate(
                    "KEY: B\nSPEECH: " + speech, scenario(answer, "ja")
                ))

    def test_em_dash_only_inside_full_literal_selected_choice(self):
        data = scenario("Paris — Lyon", "zh")
        sentence = "我选Paris — Lyon。"
        self.assertEqual(_validate(
            "KEY: B\nSPEECH: " + sentence, data
        ), sentence)
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: 我选Paris — Lyon — 就它。", data
        ))
        self.assertIsNone(_validate(
            "KEY: B\nSPEECH: 我选Paris — Lyonaise。", data
        ))


if __name__ == "__main__":
    unittest.main()
