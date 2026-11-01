"""No-network regression tests for opt-in candidate DEBUG: logging."""
import json
import os
import unittest
from time import perf_counter
from unittest.mock import Mock, patch

from fastapi import HTTPException

from diagnostics import debug_event, generation_attempt, request_id
from tagging.debug import debug_rejected_candidate
from tagging.core import HistoryItem
from test_tagged_dedup import candidate


def trace_lines(logger):
    """Decode the literal DEBUG: prefix and its JSON payload from mock calls."""
    results = []
    for call in logger.call_args_list:
        rendered = call.args[0] % call.args[1:]
        assert rendered.startswith("DEBUG: "), rendered
        results.append(json.loads(rendered[len("DEBUG: "):]))
    return results


class DebugEventTests(unittest.TestCase):
    def test_debug_is_off_by_default_even_if_logger_is_configured(self):
        with patch.dict(os.environ, {"AI_GENERATION_DEBUG": "false"}), \
             patch("diagnostics.debug_log.debug") as logger:
            debug_event("question_candidate_rejected", question="private")
            logger.assert_not_called()

    def test_debug_lines_begin_with_literal_DEBUG_and_include_context(self):
        with patch.dict(os.environ, {"AI_GENERATION_DEBUG": "true"}), \
             patch("diagnostics.debug_log.debug") as logger:
            request_token = request_id.set("request-abc")
            attempt_token = generation_attempt.set(2)
            try:
                debug_event("question_candidate_rejected", question="Пример")
            finally:
                generation_attempt.reset(attempt_token)
                request_id.reset(request_token)
        [row] = trace_lines(logger)
        self.assertEqual(row["event"], "question_candidate_rejected")
        self.assertEqual(row["request_id"], "request-abc")
        self.assertEqual(row["attempt"], 2)
        self.assertEqual(row["question"], "Пример")

    def test_candidate_content_is_bounded_and_fields_allowlisted(self):
        raw = {
            "difficulty": 5,
            "question": "Q" * 4000,
            "answers": {"A": "A" * 800, "B": "B",
                        "C": "C", "D": "D", "secret": "must-not-appear"},
            "correct_answer": "B",
            "tags": {"entity": ["physics"], "concept": ["general-science"],
                     "keyword": []},
            "fact": {"subject": "physics", "relation": "inventor",
                     "object": "albert-einstein"},
            "api_key": "NEVER_LOG_THIS",
            "user_history": "NEVER_LOG_THIS",
        }
        with patch.dict(os.environ, {"AI_GENERATION_DEBUG": "true"}), \
             patch("diagnostics.debug_log.debug") as logger:
            debug_rejected_candidate(raw, reason="invalid_question",
                                     error_type="ValidationError")
        [row] = trace_lines(logger)
        self.assertEqual(len(row["question"]), 2500)
        self.assertEqual(len(row["answers"]["A"]), 500)
        self.assertEqual(sorted(row["answers"]), ["A", "B", "C", "D"])
        self.assertEqual(row["fact"]["relation"], "inventor")
        self.assertNotIn("api_key", row)
        self.assertNotIn("user_history", row)
        self.assertNotIn("NEVER_LOG_THIS", json.dumps(row))


class DebugGenerationTests(unittest.TestCase):
    def settings(self, enabled="true", quality="audit"):
        return patch.dict(os.environ, {
            "AI_GENERATION_DEBUG": enabled,
            "AI_QUALITY_MODE": quality,
            "AI_LOCAL_LEAK_MODE": "off",
            "AI_GENERATION_MAX_ATTEMPTS": "1",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12",
            "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        })

    def test_rejected_critic_candidate_has_full_verdict_only_in_debug(self):
        from api import generate_valid_question
        verdict = {
            "facts": "fail", "options": "fail", "language": "pass",
            "leakage": "pass", "difficulty": "appropriate",
            "independent_answer": "unknown",
            "issue_codes": ["factual_error", "ambiguous"],
            "distractor_penalty": 1, "knowledge_penalty": 0,
            "suitability_penalty": 1, "wording_penalty": 1,
            "rationale": "The answer is inaccurate; another option may also fit.",
            "tags_valid": "fail", "fact_valid": "fail",
            "semantic_duplicate": False, "duplicate_match": "",
        }
        with self.settings(), \
             patch("api.create_question", return_value=candidate().model_dump()), \
             patch("tagging.review.ReviewerSettings.from_env", return_value=Mock(
                 provider="openai", model_id="critic-mock", structured_output_mode="strict",
             )), \
             patch("tagging.review.LLMClient"), \
             patch("tagging.review.completed_text", return_value=json.dumps(verdict)), \
             patch("diagnostics.debug_log.debug") as logger:
            with self.assertRaises(HTTPException) as raised:
                generate_valid_question(0, "English", "History",
                                        tag_history=[], tag_context=[])
        self.assertEqual(raised.exception.status_code, 502)
        [row] = trace_lines(logger)
        self.assertEqual(row["rejection_reason"], "quality_rejected")
        self.assertEqual(row["correct_answer"], "B")
        self.assertEqual(row["question"], candidate().question)
        self.assertEqual(row["critic"]["assessment"]["total"], 3)
        self.assertEqual(row["critic"]["verdict"]["rationale"], verdict["rationale"])
        self.assertEqual(row["critic"]["verdict"]["fact_valid"], "fail")
        self.assertIsNotNone(row["tags"])
        self.assertIsNotNone(row["fact"])
        self.assertEqual(row["critic"]["decision"], "reject")

    def test_off_mode_never_logs_rejected_candidate_bodies(self):
        from api import generate_valid_question
        invalid = candidate().model_dump()
        invalid["answers"]["D"] = invalid["answers"]["A"]
        with self.settings(enabled="false"), \
             patch("api.create_question", return_value=invalid), \
             patch("diagnostics.debug_log.debug") as logger:
            with self.assertRaises(HTTPException):
                generate_valid_question(0, "English", "History",
                                        tag_history=[], tag_context=[])
        logger.assert_not_called()

    def test_python_duplicate_logs_answer_and_reason_without_critic_call(self):
        from api import generate_valid_question
        repeated = candidate()
        history = [HistoryItem(
            question_id="old-question",
            category_slug="history",
            question_text=repeated.question,
            fact_key=repeated.fact.key,
            subject=repeated.fact.subject,
            relation=repeated.fact.relation,
            object=repeated.fact.object,
            tags={"entity": tuple(repeated.tags.entity),
                  "concept": tuple(repeated.tags.concept),
                  "keyword": tuple(repeated.tags.keyword)},
        )]
        with self.settings(), \
             patch("api.create_question", return_value=repeated.model_dump()), \
             patch("tagging.review.check_tagged_quality") as reviewer, \
             patch("diagnostics.debug_log.debug") as logger:
            with self.assertRaises(HTTPException) as raised:
                generate_valid_question(0, "English", "History",
                                        tag_history=history, tag_context=[])
        self.assertEqual(raised.exception.status_code, 409)
        reviewer.assert_not_called()
        [row] = trace_lines(logger)
        self.assertEqual(row["rejection_reason"], "duplicate_rejected")
        self.assertEqual(row["duplicate_reason"], "same_fact")
        self.assertEqual(row["python_duplicate_check"]["matched_question_id"],
                         "old-question")
        self.assertEqual(row["correct_answer"], "B")
        self.assertEqual(row["fact_key"], repeated.fact.key)
        self.assertIsNone(row["critic"])

    def test_structurally_invalid_candidate_is_logged_when_enabled(self):
        from api import generate_valid_question
        invalid = candidate().model_dump()
        invalid["answers"]["C"] = invalid["answers"]["A"]
        with self.settings(), \
             patch("api.create_question", return_value=invalid), \
             patch("diagnostics.debug_log.debug") as logger:
            with self.assertRaises(HTTPException) as raised:
                generate_valid_question(0, "English", "History",
                                        tag_history=[], tag_context=[])
        self.assertEqual(raised.exception.status_code, 502)
        [row] = trace_lines(logger)
        self.assertEqual(row["rejection_reason"], "invalid_question")
        self.assertEqual(row["error_type"], "ValidationError")
        self.assertEqual(row["answers"]["C"], invalid["answers"]["A"])

    def test_invalid_json_has_separate_debug_event(self):
        import question_generator
        with self.settings(), \
             patch("question_generator.client", return_value=Mock()), \
             patch("question_generator.completed_text", return_value='{"question": invalid'), \
             patch("diagnostics.debug_log.debug") as logger:
            with self.assertRaises(json.JSONDecodeError):
                question_generator.create_question(0, "English", "History",
                                                   tag_context=[])
        [row] = trace_lines(logger)
        self.assertEqual(row["event"], "question_candidate_invalid_json")
        self.assertEqual(row["response_excerpt"], '{"question": invalid')


    def test_accepted_candidate_logs_full_question_and_critic_verdict(self):
        from api import generate_valid_question
        verdict = {
            "facts": "pass", "options": "pass", "language": "pass",
            "leakage": "pass", "difficulty": "too_easy",
            "independent_answer": "B", "issue_codes": [],
            "distractor_penalty": 1, "knowledge_penalty": 0,
            "suitability_penalty": 1, "wording_penalty": 1,
            "rationale": "Question is valid but too easy and awkwardly phrased.",
            "tags_valid": "pass", "fact_valid": "pass",
            "semantic_duplicate": False, "duplicate_match": "",
        }
        with self.settings(), \
             patch("api.create_question", return_value=candidate().model_dump()), \
             patch("tagging.review.ReviewerSettings.from_env", return_value=Mock(
                 provider="openai", model_id="critic-mock", structured_output_mode="strict",
             )), \
             patch("tagging.review.LLMClient"), \
             patch("tagging.review.completed_text", return_value=json.dumps(verdict)), \
             patch("diagnostics.debug_log.debug") as logger:
            result = generate_valid_question(0, "English", "History",
                                             tag_history=[], tag_context=[])
        [row] = trace_lines(logger)
        self.assertEqual(row["event"], "question_candidate_accepted")
        self.assertEqual(row["outcome"], "accepted")
        self.assertIsNone(row["rejection_reason"])
        self.assertEqual(row["question"], result.question)
        self.assertEqual(row["answers"], result.answers)
        self.assertEqual(row["correct_answer"], result.correct_answer)
        self.assertEqual(row["tags"]["entity"], ["albert-einstein"])
        self.assertEqual(row["fact"]["relation"], "birth-year")
        self.assertEqual(row["fact_key"], result.fact.key)
        self.assertEqual(row["python_duplicate_check"]["score"], 0)
        self.assertEqual(row["critic"]["verdict"]["difficulty"], "too_easy")
        self.assertEqual(row["critic"]["verdict"]["rationale"], verdict["rationale"])
        self.assertEqual(row["critic"]["assessment"]["total"], 3.25)
        self.assertEqual(row["critic"]["assessment"]["penalties"]["knowledge"], 0.25)
        self.assertEqual(row["critic"]["assessment"]["score_threshold"], 3.25)
        # With AI_QUALITY_MODE=audit the editor's criticism does not block.
        self.assertEqual(row["critic"]["decision"], "audit")

    def test_accepted_candidate_never_leaks_to_logs_when_debug_off(self):
        from api import generate_valid_question
        with self.settings(enabled="false"), \
             patch("api.create_question", return_value=candidate().model_dump()), \
             patch("tagging.review.check_tagged_quality"), \
             patch("diagnostics.debug_log.debug") as logger:
            result = generate_valid_question(0, "English", "History",
                                             tag_history=[], tag_context=[])
        self.assertEqual(result.question, candidate().question)
        logger.assert_not_called()

    def test_rejected_then_accepted_attempts_each_have_one_debug_record(self):
        from api import generate_valid_question
        invalid = candidate().model_dump()
        invalid["answers"]["C"] = invalid["answers"]["A"]
        valid = candidate().model_dump()
        with self.settings(), \
             patch.dict(os.environ, {"AI_GENERATION_MAX_ATTEMPTS": "2"}), \
             patch("api.create_question", side_effect=[invalid, valid]), \
             patch("api.sleep"), \
             patch("tagging.review.check_tagged_quality"), \
             patch("diagnostics.debug_log.debug") as logger:
            token = request_id.set("attempts-123")
            try:
                generated = generate_valid_question(
                    0, "English", "History", tag_history=[], tag_context=[]
                )
            finally:
                request_id.reset(token)
        rows = trace_lines(logger)
        self.assertEqual([row["event"] for row in rows], [
            "question_candidate_rejected", "question_candidate_accepted",
        ])
        self.assertEqual([row["outcome"] for row in rows], ["rejected", "accepted"])
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertTrue(all(row["request_id"] == "attempts-123" for row in rows))
        self.assertEqual(rows[1]["question"], generated.question)
        self.assertEqual(rows[1]["correct_answer"], "B")
