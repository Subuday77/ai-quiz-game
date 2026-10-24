"""Conservative local leak policy: offline regression and API integration."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

from api import app, sessions
from local_leak_filter import (exact_answer_leak, local_leak_mode_from_env,
                               check_local_leak)
from quality_errors import QualityRegenerationError
from question_schema import QuizQuestion


def question(stem, answer, *, key="B", difficulty=11):
    others = {"A": "Телефон", "B": answer, "C": "Компас", "D": "Пароход"}
    return QuizQuestion.model_validate({
        "difficulty": difficulty,
        "question": stem,
        "answers": others,
        "correct_answer": key,
    })


class LiteralLeakTests(unittest.TestCase):
    def test_exact_answer_repeated_as_city_and_title(self):
        self.assertTrue(exact_answer_leak(question(
            "Какой древний город был столицей царства Эбла?",
            "Эбла")))
        self.assertTrue(exact_answer_leak(question(
            "Какой роман Вирджинии Вулф начинается словами "
            "«Миссис Дэллоуэй сама купит цветы»?",
            "Миссис Дэллоуэй")))

    def test_unicode_punctuation_and_diacritics(self):
        self.assertTrue(exact_answer_leak(question(
            "Какое произведение называется «café noir»?", "Cafe noir")))
        self.assertTrue(exact_answer_leak(question(
            "Как называется ЭБЛА?", "Эбла")))

    def test_quotes_are_not_automatically_leaks(self):
        self.assertFalse(exact_answer_leak(question(
            "С каким телесериалом связан слоган «The truth is out there»?",
            "Секретные материалы")))
        self.assertFalse(exact_answer_leak(question(
            "Какой японский праздник связан с цветением сакуры?",
            "Ханами")))
        self.assertFalse(exact_answer_leak(question(
            "На каком инструменте 88 клавиш?", "Пианино", difficulty=0)))

    def test_substrings_morphology_and_tiny_tokens_not_guessed(self):
        self.assertFalse(exact_answer_leak(question(
            "Кем разработано уравнение Дирака?", "Дирак")))
        self.assertFalse(exact_answer_leak(question(
            "Какую птицу имел в виду автор, говоря об Орле?", "Орёл")))
        self.assertFalse(exact_answer_leak(question(
            "Что изображено на карте Рима?", "Рим")))
        self.assertFalse(exact_answer_leak(question(
            "Что показывает слово «импрессионизм»?", "Импрессион", difficulty=1)))

    def test_no_unique_match_when_wrong_choice_also_contains_phrase(self):
        q = question("Что означает слово «Эбла»?", "Эбла")
        q = q.model_copy(update={"answers": {**q.answers, "A": "Древняя Эбла"}})
        self.assertFalse(exact_answer_leak(q))

    def test_past_good_game_questions_are_not_blocked(self):
        # Regression: false semantic "leaks" were seen for iconic slogans.
        from test_openai_critic_v3_calibration import REAL_GAME_CASES, question_from_case
        for case in REAL_GAME_CASES:
            with self.subTest(case=case["id"]):
                self.assertFalse(exact_answer_leak(question_from_case(case)))

    def test_modes_and_invalid_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(local_leak_mode_from_env(), "enforce")
        for name in ("off", "audit", "enforce"):
            with patch.dict(os.environ, {"AI_LOCAL_LEAK_MODE": name}):
                self.assertEqual(local_leak_mode_from_env(), name)
        with patch.dict(os.environ, {"AI_LOCAL_LEAK_MODE": "INVALID"}):
            with self.assertRaises(ValueError):
                local_leak_mode_from_env()

    def test_independent_gate_has_no_reviewer_dependency(self):
        q = question("Столица царства Эбла — какой город?", "Эбла")
        with self.assertRaises(QualityRegenerationError):
            check_local_leak(q, "enforce")
        check_local_leak(q, "audit")
        check_local_leak(q, "off")


class PublicationGateTests(unittest.TestCase):
    def setUp(self):
        sessions.clear()
        self.http = TestClient(app)
        self.enterContext(patch("api.sleep"))
        self.env = self.enterContext(patch.dict(os.environ, {
            "AI_QUALITY_MODE": "off", "AI_LOCAL_LEAK_MODE": "enforce",
            "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12",
        }))
        self.game_id = self.http.post("/api/games", json={"language": "Russian"}).json()["game_id"]
        self.url = f"/api/games/{self.game_id}/question"
        self.leaky = {
            "difficulty": 0, "question": "Какой город — Эбла?",
            "answers": {"A": "Рим", "B": "Эбла", "C": "Афины", "D": "Спарта"},
            "correct_answer": "B",
        }
        self.clean = {
            **self.leaky,
            "question": "Какой город был столицей древнего царства в Сирии?",
        }

    def test_rejected_candidate_never_reaches_client_when_critic_off(self):
        with patch("api.create_question", return_value=self.leaky) as generated, patch(
                "api.check_quality") as reviewer:
            response = self.http.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(generated.call_count, 3)
        reviewer.assert_not_called()
        self.assertIsNone(sessions[self.game_id].question)
        self.assertEqual(sessions[self.game_id].state.current_difficulty, 0)

    def test_local_leak_then_clean_candidate_uses_existing_retry(self):
        with patch("api.create_question", side_effect=[self.leaky, self.clean]) as generated:
            response = self.http.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(generated.call_count, 2)
        self.assertEqual(response.json()["question"], self.clean["question"])
        self.assertNotIn("correct_answer", response.json())

    def test_critic_v3_audit_does_not_override_independent_gate(self):
        with patch.dict(os.environ, {"AI_QUALITY_MODE": "audit",
                                      "AI_QUALITY_CONTRACT": "v3"}), patch(
                "api.create_question", return_value=self.leaky) as generated, patch(
                "api.check_quality") as model_review:
            response = self.http.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(generated.call_count, 3)
        model_review.assert_not_called()

    def test_audit_override_does_not_block_publication(self):
        with patch.dict(os.environ, {"AI_LOCAL_LEAK_MODE": "audit"}), patch(
                "api.create_question", return_value=self.leaky):
            response = self.http.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_invalid_mode_fails_before_generation(self):
        with patch.dict(os.environ, {"AI_LOCAL_LEAK_MODE": "WRONG"}), patch(
                "api.create_question") as generated:
            response = self.http.get(self.url)
        self.assertEqual(response.status_code, 503)
        generated.assert_not_called()


if __name__ == "__main__":
    unittest.main()
