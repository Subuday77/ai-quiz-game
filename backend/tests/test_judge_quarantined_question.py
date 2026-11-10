"""Read-only live-model probe must never create or settle a player appeal."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from scripts.judge_quarantined_question import evaluate


class JudgeQuarantinedProbeTests(unittest.TestCase):
    def test_quarantined_question_probe_calls_both_models_without_db_writes(self):
        qid = uuid4()
        question = SimpleNamespace(
            id=qid, status="quarantined", requested_difficulty=9,
            text="Who won the 1972 Turing Award for COBOL?",
            choices={"A": "Backus", "B": "Hopper", "C": "Dijkstra", "D": "Knuth"},
            correct_key="B", language="Russian", generator_model="gpt-5.4",
        )
        with (patch("scripts.judge_quarantined_question.engine"),
              patch("scripts.judge_quarantined_question.Session") as session,
              patch("scripts.judge_quarantined_question._config",
                    side_effect=lambda role: (role, "low", 800, 30.0)),
              patch("scripts.judge_quarantined_question.keddy_messages",
                    return_value=[{"role": "user", "content": "evidence"}]),
              patch("scripts.judge_quarantined_question.judge_messages",
                    return_value=[{"role": "user", "content": "judge evidence"}]) as judge_messages,
              patch("scripts.judge_quarantined_question._completion",
                    side_effect=["keddy output", "judge output"]) as completion,
              patch("scripts.judge_quarantined_question.parse_keddy") as parse_keddy,
              patch("scripts.judge_quarantined_question.parse_judge") as parse_judge):
            db = session.return_value.__enter__.return_value
            db.get.return_value = question
            parse_keddy.return_value.model_dump.return_value = {"summary_en": "needs checking"}
            parse_judge.return_value.model_dump.return_value = {
                "decision": "upheld", "explanation_player_language": "Нет верного ответа"}
            data = evaluate(qid)
            self.assertEqual(data["mode"], "READ_ONLY_MODEL_PROBE")
            self.assertEqual(data["judge"]["decision"], "upheld")
            self.assertEqual(data["question"]["stored_correct_key"], "B")
            self.assertEqual(data["question"]["question_number"], 10)
            self.assertFalse(data["appeal_created"])
            self.assertFalse(data["settlement_executed"])
            db.execute.assert_called_once()
            self.assertEqual(str(db.execute.call_args.args[0]), "SET TRANSACTION READ ONLY")
            db.commit.assert_not_called()
            db.add.assert_not_called()
            db.rollback.assert_called_once()
            self.assertEqual(completion.call_count, 2)
            self.assertEqual(
                judge_messages.call_args.kwargs["correct_key"], "B")

    def test_refuses_questions_not_in_quarantine(self):
        with (patch("scripts.judge_quarantined_question.engine"),
              patch("scripts.judge_quarantined_question.Session") as session,
              patch("scripts.judge_quarantined_question._completion") as completion):
            db = session.return_value.__enter__.return_value
            db.get.return_value = SimpleNamespace(status="approved")
            with self.assertRaisesRegex(ValueError, "Only quarantined"):
                evaluate(uuid4())
            completion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
