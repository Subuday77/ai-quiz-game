"""Regression guards for strict schemas, privacy-safe logging and appeals UI API."""
import io
import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from persistence.appeal_ai_security import response_json_schema
from persistence.appeal_logging import LOGGER, emit, validation_summary
from persistence.appeal_routes import _public, router
from persistence.appeal_worker import _completion


class AppealRevisionTests(unittest.TestCase):
    def test_strict_model_output_contracts(self):
        for role, expected in (
            ("CADDY", {"summary_en", "claims_to_check", "identified_issues",
                       "needs_external_fact_check"}),
            ("JUDGE", {"decision", "explanation_en",
                       "explanation_player_language", "rationale_facts"}),
        ):
            with self.subTest(role=role):
                schema = response_json_schema(role)
                self.assertEqual(schema["type"], "object")
                self.assertEqual(schema["additionalProperties"], False)
                self.assertEqual(set(schema["required"]), expected)
                self.assertEqual(set(schema["properties"]), expected)

    def test_api_exposes_private_monthly_allowance(self):
        paths = {route.path for route in router.routes}
        self.assertIn("/api/v2/appeals/allowance", paths)

    def test_before_answer_history_does_not_leak_private_key(self):
        obj = SimpleNamespace(
            id="a", question_id="q", run_id=None,
            question_snapshot={"question": "Q?", "answers": {"A": "yes"}},
            language="English", reasons=[], comment="x", source_links=[],
            before_answer=True, question_number=1, status="retry_pending",
            submitted_at=None, closed_at=None, verdict_localized=None,
            deposit_tier="bronze", deposit_refunded_at=None,
            filing_fee_bronze=0, compensated_at=None,
            bonus_wager_refunded_at=None, verdict_data=None,
            context={"correct_key": "B", "selected_key": None},
        )
        payload = _public(obj)
        self.assertIsNone(payload["answer_key"])
        self.assertIsNone(payload["selected_key"])
        self.assertNotIn("correct_key", json.dumps(payload))
        obj.before_answer = False
        obj.context["selected_key"] = "A"
        payload = _public(obj)
        self.assertEqual(payload["answer_key"], "B")
        self.assertEqual(payload["selected_key"], "A")

    def test_info_and_debug_log_scrub_data(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)
        try:
            with patch.dict(os.environ, {"QUIZ_COMPLAINT_LOG_LEVEL": "DEBUG"}):
                emit("DEBUG", "appeal_validation_detail", appeal_id="case-1",
                     stage="keddy_validation", error_code="pydantic",
                     validation_fields=["claims_to_check:missing"],
                     comment="PRIVATE COMMENT", correct_key="B",
                     api_key="super-secret")
            record = json.loads(output.getvalue().strip())
            self.assertEqual(record["appeal_id"], "case-1")
            self.assertEqual(record["validation_fields"], ["claims_to_check:missing"])
            self.assertNotIn("PRIVATE COMMENT", output.getvalue())
            self.assertNotIn("super-secret", output.getvalue())
            self.assertNotIn("correct_key", output.getvalue())
        finally:
            LOGGER.removeHandler(handler)
            LOGGER.setLevel(logging.INFO)

    def test_completion_uses_strict_schema_for_both_roles(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "fake",
            "QUIZ_COMPLAINT_CADDY_MODEL_ID": "gpt-6-luna",
            "QUIZ_COMPLAINT_CADDY_REASONING_EFFORT": "low",
            "QUIZ_COMPLAINT_CADDY_MAX_OUTPUT_TOKENS": "800",
            "QUIZ_COMPLAINT_CADDY_TIMEOUT_SECONDS": "12",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "gpt-6.1-sol",
            "QUIZ_COMPLAINT_JUDGE_REASONING_EFFORT": "medium",
            "QUIZ_COMPLAINT_JUDGE_MAX_OUTPUT_TOKENS": "1800",
            "QUIZ_COMPLAINT_JUDGE_TIMEOUT_SECONDS": "40",
        }), patch("persistence.appeal_worker.OpenAI") as client:
            client.return_value.responses.create.return_value = SimpleNamespace(
                status="completed", output_text='{"result":"mock"}', usage=None,
                incomplete_details=None,
            )
            for role in ("CADDY", "JUDGE"):
                _completion([{"role": "system", "content": "Return JSON"}], role)
                spec = client.return_value.responses.create.call_args.kwargs["text"]["format"]
                self.assertEqual(spec["type"], "json_schema")
                self.assertTrue(spec["strict"])
                self.assertEqual(spec["schema"]["required"],
                                 list(response_json_schema(role)["properties"]))


if __name__ == "__main__":
    unittest.main()
