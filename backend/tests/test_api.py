"""Offline integration tests: never call a paid or remote AI provider."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app, sessions


QUESTION = {
    "difficulty": 0,
    "question": "Which planet is known as the Red Planet?",
    "answers": {"A": "Venus", "B": "Mars", "C": "Jupiter", "D": "Mercury"},
    "correct_answer": "B",
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        sessions.clear()
        self.enterContext(patch("api.sleep"))
        self.enterContext(patch.dict("os.environ", {
            "AI_GENERATION_MAX_ATTEMPTS": "3", "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12", "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        }))
        self.client = TestClient(app)
        self.id = self.client.post("/api/games", json={"language": "English"}).json()["game_id"]

    @patch("api.create_question", return_value=QUESTION)
    def test_correct_answer_is_not_leaked_and_question_is_cached(self, generated):
        url = f"/api/games/{self.id}/question"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertNotIn("correct_answer", first.json())
        self.assertEqual(first.json(), self.client.get(url).json())
        generated.assert_called_once()
        answer = self.client.post(f"/api/games/{self.id}/answer",
                                  json={"question_id": first.json()["question_id"], "answer": "B"})
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.json()["correct"])
        self.assertEqual(answer.json()["correct_answer"], "B")
        replay = self.client.post(f"/api/games/{self.id}/answer",
                                  json={"question_id": first.json()["question_id"], "answer": "B"})
        self.assertEqual(replay.status_code, 409)

    @patch("api.create_question")
    def test_invalid_question_retries_up_to_three_times(self, generated):
        generated.side_effect = [
            {**QUESTION, "answers": {"A": "Venus"}},
            {**QUESTION, "difficulty": 4},
            QUESTION,
        ]
        response = self.client.get(f"/api/games/{self.id}/question")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(generated.call_count, 3)

    @patch("api.create_question")
    def test_invalid_question_returns_gateway_error(self, generated):
        generated.return_value = {**QUESTION, "correct_answer": "X"}
        response = self.client.get(f"/api/games/{self.id}/question")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(generated.call_count, 3)

    @patch("api.create_question")
    def test_perfect_run_unlocks_platinum_on_question_16(self, generated):
        generated.side_effect = lambda difficulty, language, category: {
            **QUESTION, "difficulty": difficulty,
        }
        awards = []
        for number in range(1, 17):
            question = self.client.get(f"/api/games/{self.id}/question")
            self.assertEqual(question.status_code, 200)
            self.assertEqual(question.json()["number"], number)
            self.assertEqual(question.json()["bonus"], number == 16)
            result = self.client.post(f"/api/games/{self.id}/answer", json={
                "question_id": question.json()["question_id"], "answer": "B",
            })
            self.assertEqual(result.status_code, 200)
            if result.json()["achievement"]:
                awards.append(result.json()["achievement"])
        self.assertEqual(awards, ["bronze", "silver", "gold", "platinum"])
        self.assertEqual(result.json()["phase"], "completed")
        self.assertEqual(generated.call_count, 16)
        self.assertEqual(self.client.get(f"/api/games/{self.id}/question").status_code, 409)

    @patch("api.create_question", side_effect=RuntimeError("Provider down"))
    def test_provider_error_is_not_retried(self, generated):
        result = self.client.get(f"/api/games/{self.id}/question")
        self.assertEqual(result.status_code, 503)
        generated.assert_called_once()

    def test_reject_unsupported_language(self):
        result = self.client.post("/api/games", json={"language": "Klingon"})
        self.assertEqual(result.status_code, 422)

    def test_health(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
