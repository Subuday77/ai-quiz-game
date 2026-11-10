"""Fast offline guards for the appeal HTTP / model integration."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from persistence.appeal_routes import router as appeal_router
from persistence.appeal_ai_security import parse_judge
from persistence.appeal_routes import AppealInput, _public
from persistence.appeal_worker import _config, _completion, _evidence


class AppealIntegrationTests(unittest.TestCase):
    def test_private_api_is_registered(self):
        paths = {(method, route.path) for route in appeal_router.routes
                 for method in getattr(route, "methods", ())}
        for pair in (("GET", "/api/v2/games/{run_id}/appeals/{question_id}/quote"),
                     ("POST", "/api/v2/games/{run_id}/appeals"),
                     ("GET", "/api/v2/appeals"),
                     ("GET", "/api/v2/appeals/{appeal_id}")):
            self.assertIn(pair, paths)

    def test_submit_requires_exact_quoted_fee(self):
        import uuid
        with self.assertRaises(Exception):
            AppealInput(question_id=uuid.uuid4(), request_id=uuid.uuid4(),
                        reasons=["ambiguous"], comment="bad")
        self.assertEqual(AppealInput(
            question_id=uuid.uuid4(), request_id=uuid.uuid4(),
            reasons=["ambiguous"], expected_fee_bronze=0).expected_fee_bronze, 0)

    def test_player_response_never_leaks_correct_key(self):
        obj = SimpleNamespace(
            id="a", question_id="q", run_id="r",
            question_snapshot={"question": "2+2?", "answers": {"A": "3", "B": "4"}},
            language="English", reasons=["ambiguous"], comment="test",
            source_links=[], before_answer=True, question_number=1,
            status="pending", submitted_at=None, closed_at=None,
            verdict_localized=None, deposit_tier="bronze",
            deposit_refunded_at=None, filing_fee_bronze=0,
            compensated_at=None, bonus_wager_refunded_at=None, verdict_data=None,
            context={"correct_key": "B"})
        response = _public(obj)
        self.assertNotIn("correct_key", json.dumps(response))
        self.assertNotIn("context", response)

    def test_evidence_has_four_answers_and_play_context(self):
        obj = SimpleNamespace(
            question_snapshot={"question": "2+2?", "answers": {
                "A": "3", "B": "4", "C": "5", "D": "6"},
                "hidden_keys": ["A"]},
            context={"correct_key": "B", "hints_used": 1, "selected_key": "A",
                     "run_status_at_submission": "lost",
                     "bonus_wager_accepted": False},
            reasons=["ambiguous"], comment="not clear", source_links=[],
            question_number=3, language="English", before_answer=False)
        item = _evidence(obj)
        self.assertEqual(list(item["answers"]), list("ABCD"))
        self.assertNotIn("correct_key", json.dumps(item))
        self.assertEqual(item["round_context"]["hidden_keys"], ["A"])

    def test_keddy_and_judge_config_and_distinct_calls(self):
        with patch.dict(os.environ, {
            "QUIZ_COMPLAINT_CADDY_MODEL_ID": "gpt-6-luna",
            "QUIZ_COMPLAINT_CADDY_REASONING_EFFORT": "low",
            "QUIZ_COMPLAINT_CADDY_MAX_OUTPUT_TOKENS": "800",
            "QUIZ_COMPLAINT_CADDY_TIMEOUT_SECONDS": "12",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "gpt-6.1-sol",
            "QUIZ_COMPLAINT_JUDGE_REASONING_EFFORT": "medium",
            "QUIZ_COMPLAINT_JUDGE_MAX_OUTPUT_TOKENS": "1800",
            "QUIZ_COMPLAINT_JUDGE_TIMEOUT_SECONDS": "40",
            "OPENAI_API_KEY": "fake-test-key",
        }):
            self.assertNotEqual(_config("CADDY")[0], _config("JUDGE")[0])
            data = json.dumps({
                "decision": "inconclusive", "explanation_en": "Uncertain",
                "explanation_player_language": "Uncertain", "rationale_facts": []})
            mock_result = SimpleNamespace(status="completed", output_text=data)
            with patch("persistence.appeal_worker.OpenAI") as client:
                client.return_value.responses.create.return_value = mock_result
                self.assertEqual(parse_judge(_completion(
                    [{"role": "system", "content": "Return JSON"}], "JUDGE"
                )).decision, "inconclusive")
                kwargs = client.return_value.responses.create.call_args.kwargs
                self.assertEqual(kwargs["model"], "gpt-6.1-sol")
                self.assertEqual(kwargs["reasoning"], {"effort": "medium"})


if __name__ == "__main__":
    unittest.main()
