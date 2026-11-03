"""Transactional star economy tests; SQLite offline, no OpenAI calls."""
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from persistence.gameplay import hidden_choices, hide_wrong_options, skip_main_question
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, QuestionReview, StarAward,
    Topic, User, WalletTransaction,
)
from persistence.star_economy import exchange_stars, star_balance
from star_perks import PerkUnavailable


class StarEconomyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(username="tester", username_key=f"tester-{uuid4()}")
        self.db.add(self.user)
        self.db.commit()
        self.user_id = self.user.id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def award(self, tier):
        run = GameRun(user_id=self.user_id, language="English", status="completed")
        self.db.add(run)
        self.db.flush()
        self.db.add(StarAward(user_id=self.user_id, run_id=run.id, tier=tier))
        self.db.commit()

    def credit(self, tier, count):
        self.db.add(WalletTransaction(
            user_id=self.user_id, idempotency_key=str(uuid4()),
            reason=f"star/{tier}/test-credit", amount=count,
        ))
        self.db.commit()

    def active_question(self):
        topic = Topic(slug=f"test-{uuid4().hex}", labels={})
        self.db.add(topic)
        self.db.flush()
        question = Question(
            topic_id=topic.id, language="English", requested_difficulty=0,
            text="Which answer is right?", choices={"A": "bad 1", "B": "correct",
                                                     "C": "bad 2", "D": "bad 3"},
            correct_key="B", fingerprint=uuid4().hex, status="approved",
            generator_provider="test", generator_model="test",
            prompt_version="test", schema_version="test",
        )
        run = GameRun(user_id=self.user_id, language="English", status="main")
        self.db.add_all([question, run])
        self.db.flush()
        attempt = QuestionAttempt(
            run_id=run.id, question_id=question.id,
            number=1, requested_difficulty=0,
        )
        self.db.add(attempt)
        self.db.commit()
        return run.id, question.id, attempt.id

    def test_exchange_atomic_and_idempotent(self):
        for _ in range(10):
            self.award("bronze")
        request_id = uuid4()
        balance = exchange_stars(self.db, user_id=self.user_id,
                                 source="bronze", target="silver",
                                 amount=10, request_id=request_id)
        self.assertEqual(balance["bronze"], 0)
        self.assertEqual(balance["silver"], 1)
        replay = exchange_stars(self.db, user_id=self.user_id,
                                source="bronze", target="silver",
                                amount=10, request_id=request_id)
        self.assertEqual(replay, balance)
        with self.assertRaises(PerkUnavailable):
            exchange_stars(self.db, user_id=self.user_id,
                           source="bronze", target="gold",
                           amount=100, request_id=request_id)
        self.assertEqual(star_balance(self.db, self.user_id, "silver"), 1)
        exchange_stars(self.db, user_id=self.user_id, source="silver",
                       target="bronze", amount=1, request_id=uuid4())
        self.assertEqual(star_balance(self.db, self.user_id, "bronze"), 10)

    def test_failed_exchange_preserves_balance(self):
        self.award("silver")
        with self.assertRaises(PerkUnavailable):
            exchange_stars(self.db, user_id=self.user_id, source="gold",
                           target="silver", amount=1, request_id=uuid4())
        self.assertEqual(star_balance(self.db, self.user_id, "gold"), 0)
        self.assertEqual(star_balance(self.db, self.user_id, "silver"), 1)

    def test_skip_is_single_use_and_does_not_score(self):
        self.award("gold")
        run_id, question_id, attempt_id = self.active_question()
        operation_id = uuid4()
        result = skip_main_question(
            self.db, run_id=run_id, user_id=self.user_id,
            question_id=question_id, request_id=operation_id,
        )
        self.assertEqual(result.correct_main_answers, 0)
        self.assertEqual(result.main_question_index, 1)
        self.assertEqual(result.hints_used, 1)
        saved = self.db.get(QuestionAttempt, attempt_id)
        self.assertIsNotNone(saved.answered_at)
        self.assertIsNone(saved.selected_key)
        self.assertIsNone(saved.is_correct)
        self.assertEqual(star_balance(self.db, self.user_id, "gold"), 0)
        retry = skip_main_question(
            self.db, run_id=run_id, user_id=self.user_id,
            question_id=question_id, request_id=operation_id,
        )
        self.assertEqual(retry.main_question_index, 1)
        self.assertEqual(star_balance(self.db, self.user_id, "gold"), 0)
        # The API-visible counters are reconstructed from persisted attempts,
        # not inferred from correct answers (which stay at zero after a skip).
        from persistence.game_routes import _game_info
        self.db.expire_all()
        summary = _game_info(self.db, self.db.get(GameRun, run_id))
        self.assertEqual(summary.main_question_index, 1)
        self.assertEqual(summary.skipped_questions, 1)
        self.assertEqual(summary.correct_main_answers, 0)

    def test_bronze_hint_removes_only_false_answers_and_survives_reload(self):
        self.credit("bronze", 3)
        run_id, question_id, attempt_id = self.active_question()
        self.db.add(QuestionReview(
            question_id=question_id, checker_type="generator_plausibility",
            contract_version="generator_plausibility_v1",
            decision="unverified_estimate",
            issues=[], details={"scores": {"A": 20, "B": 100, "C": 40, "D": 50}},
        ))
        self.db.commit()
        attempt, question = hide_wrong_options(
            self.db, run_id=run_id, user_id=self.user_id,
            question_id=question_id, count=2, request_id=uuid4(),
        )
        self.assertEqual(attempt.hints_used, 2)
        self.assertEqual(hidden_choices(self.db, attempt, question), ("A", "C"))
        self.assertEqual(star_balance(self.db, self.user_id, "bronze"), 0)
        self.db.expire_all()
        saved = self.db.get(QuestionAttempt, attempt_id)
        stored = self.db.get(Question, question_id)
        self.assertEqual(hidden_choices(self.db, saved, stored), ("A", "C"))
        with self.assertRaises(PerkUnavailable):
            hide_wrong_options(
                self.db, run_id=run_id, user_id=self.user_id,
                question_id=question_id, count=1, request_id=uuid4(),
            )


if __name__ == "__main__":
    unittest.main()
