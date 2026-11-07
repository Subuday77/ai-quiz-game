import unittest
from types import SimpleNamespace
from platinum_helper.responder import _validate, _prompt, talk, HelperUnavailable

def sample(outcome="MISTAKEN"):
    return dict(name="Sam", profession="Firefighter", language="Russian",
                topic="History", outcome=outcome, delivery_style="CONFIDENT",
                letter="A" if outcome == "MISTAKEN" else "C",
                question_text="В каком году?",
                choices={"A":"395","B":"410","C":"476","D":"527"},
                correct_option_text="476")

class FakeClient:
    def __init__(self, items):
        self.items=iter(items)
        self.calls=0
        self.responses=self
    def with_options(self, **kwargs):
        assert kwargs["timeout"] == 8.0 and kwargs["max_retries"] == 0
        return self
    def create(self, **kwargs):
        self.calls += 1
        value=next(self.items)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(status="completed",output_text=value)

class HelperResponderTests(unittest.TestCase):
    def test_valid_mistake_and_no_answer_leak(self):
        self.assertEqual(_validate("KEY: A\nSPEECH: Наверное, 395.",sample()),"Наверное, 395.")
        self.assertIsNone(_validate("KEY: C\nSPEECH: 476.",sample()))
        self.assertIsNone(_validate("KEY: A\nSPEECH: Впрочем, 476.",sample()))

    def test_confidence_is_independent_of_correctness(self):
        for outcome in ("KNOW", "MISTAKEN"):
            for style in ("CONFIDENT", "TENTATIVE"):
                data = sample(outcome)
                data["delivery_style"] = style
                instructions, prompt = _prompt(data)
                self.assertIn("KEY: X", instructions)
                self.assertIn("В каком году?", prompt)
                if style == "CONFIDENT":
                    self.assertIn("Do not hedge", instructions)
                    self.assertNotIn("Sound naturally uncertain", instructions)
                else:
                    self.assertIn("Sound naturally uncertain", instructions)
                    self.assertNotIn("Do not hedge", instructions)
                self.assertIn(f"option {data['letter']}", instructions)

    def test_unsure_has_no_question(self):
        instructions, prompt=_prompt(sample("UNSURE"))
        self.assertIn("History",prompt)
        self.assertNotIn("В каком году?",prompt)
        self.assertNotIn("476",prompt)
        self.assertEqual(_validate("SPEECH: Не знаю.",sample("UNSURE")),"Не знаю.")

    def test_bounded_retry_keeps_the_same_wrong_answer(self):
        client=FakeClient([TimeoutError(), "KEY: A\nSPEECH: Вероятно, 395."])
        self.assertEqual(talk(sample(),client=client),"Вероятно, 395.")
        self.assertEqual(client.calls,2)

    def test_exhaustion_is_not_success(self):
        client=FakeClient([TimeoutError(),TimeoutError()])
        with self.assertRaises(HelperUnavailable):
            talk(sample(),client=client)
        self.assertEqual(client.calls,2)

if __name__=="__main__":
    unittest.main()
