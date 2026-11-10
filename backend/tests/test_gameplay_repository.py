"""SQLite unit tests for the PostgreSQL gameplay transaction boundary.

A live PostgreSQL concurrency test is still required before route cutover.
"""
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from persistence.gameplay import (
    GameConflict, GameNotFound, TopicsNotSeeded, create_game,
    game_state, offer_question, owned_game, pending_question, record_answer,
)
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, StarAward, Topic, User,
)
from persistence.seed_topics import category_slug
from question_schema import QuizQuestion


class GameplayRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            poolclass=StaticPool, connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def enable_fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)

        first = User(username="Alice", username_key="alice")
        second = User(username="Bob", username_key="bob")
        self.db.add_all([first, second, Topic(
            slug=category_slug("History"), labels={"en": "History"}
        )])
        self.db.commit()
        self.first = first.id
        self.second = second.id
        self.run = create_game(self.db, user_id=self.first, language="English")

    def candidate(self, difficulty=0, suffix="0"):
        return QuizQuestion(
            difficulty=difficulty,
            question=f"What is the identifier of sample question {suffix}?",
            answers={"A": "One", "B": "Two", "C": "Three", "D": "Four"},
            correct_answer="B",
        )

    def offer(self, difficulty=0, suffix="0"):
        return offer_question(
            self.db, run_id=self.run.id, user_id=self.first,
            question=self.candidate(difficulty, suffix),
            category="History", provider="test", model="stub",
        )

    def test_round_persists_and_is_owned_by_user(self):
        self.assertEqual(owned_game(
            self.db, run_id=self.run.id, user_id=self.first
        ).status, "main")
        with self.assertRaises(GameNotFound):
            owned_game(self.db, run_id=self.run.id, user_id=self.second)
        with Session(self.engine) as fresh_db:
            saved = owned_game(
                fresh_db, run_id=self.run.id, user_id=self.first
            )
            self.assertEqual(game_state(saved).current_difficulty, 0)

    def test_question_is_cached_and_survives_new_session(self):
        attempt, question = self.offer()
        again_attempt, again_question = self.offer(suffix="other")
        self.assertEqual(attempt.id, again_attempt.id)
        self.assertEqual(question.id, again_question.id)
        with Session(self.engine) as fresh_db:
            restored = pending_question(fresh_db, run_id=self.run.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored[0].id, attempt.id)
            self.assertEqual(restored[1].correct_key, "B")
        self.assertEqual(self.db.scalar(
            select(func.count(QuestionAttempt.id))
        ), 1)

    def test_answer_commits_once_and_never_awards_on_replay(self):
        _, question = self.offer()
        result, actual_key = record_answer(
            self.db, run_id=self.run.id, user_id=self.first,
            question_id=question.id, selected="B",
        )
        self.assertTrue(result.correct)
        self.assertEqual(actual_key, "B")
        with self.assertRaises(GameConflict):
            record_answer(
                self.db, run_id=self.run.id, user_id=self.first,
                question_id=question.id, selected="B",
            )
        self.assertIsNone(pending_question(self.db, run_id=self.run.id))
        self.assertEqual(owned_game(
            self.db, run_id=self.run.id, user_id=self.first
        ).correct_main_answers, 1)

    def test_other_user_cannot_answer(self):
        _, question = self.offer()
        with self.assertRaises(GameNotFound):
            record_answer(
                self.db, run_id=self.run.id, user_id=self.second,
                question_id=question.id, selected="B",
            )
        self.assertIsNotNone(pending_question(self.db, run_id=self.run.id))

    def test_wrong_question_id_cannot_consume_attempt(self):
        self.offer()
        with self.assertRaises(GameConflict):
            record_answer(
                self.db, run_id=self.run.id, user_id=self.first,
                question_id=uuid4(), selected="B",
            )
        self.assertIsNotNone(pending_question(self.db, run_id=self.run.id))

    def test_wrong_answer_loses_and_records_duration(self):
        _, question = self.offer()
        result, _ = record_answer(
            self.db, run_id=self.run.id, user_id=self.first,
            question_id=question.id, selected="A",
        )
        self.assertFalse(result.correct)
        self.assertEqual(result.phase, "lost")
        saved = owned_game(self.db, run_id=self.run.id, user_id=self.first)
        self.assertIsNotNone(saved.finished_at)
        self.assertEqual(saved.round_score, 0)  # Not decided by gameplay contract.
        with Session(self.engine) as fresh_db:
            attempt = fresh_db.scalar(select(QuestionAttempt))
            self.assertFalse(attempt.is_correct)
            self.assertGreaterEqual(attempt.answer_duration_ms, 0)

    def test_five_correct_answers_award_bronze_once(self):
        for index in range(5):
            _, question = self.offer(difficulty=index, suffix=str(index))
            outcome, _ = record_answer(
                self.db, run_id=self.run.id, user_id=self.first,
                question_id=question.id, selected="B",
            )
        self.assertEqual(outcome.achievement, "bronze")
        self.assertEqual(self.db.scalar(
            select(func.count(StarAward.id))
        ), 1)
        self.assertEqual(self.db.scalar(
            select(func.count(QuestionAttempt.id))
        ), 5)
        self.assertEqual(self.db.scalar(
            select(GameRun.correct_main_answers)
        ), 5)

    def test_wrong_difficulty_is_rejected_without_mutation(self):
        with self.assertRaises(GameConflict):
            self.offer(difficulty=8)
        self.assertIsNone(pending_question(self.db, run_id=self.run.id))

    def test_cannot_offer_without_seeded_category(self):
        with self.assertRaises(TopicsNotSeeded):
            offer_question(
                self.db, run_id=self.run.id, user_id=self.first,
                question=self.candidate(), category="Not Seeded",
                provider="test", model="stub",
            )
        self.assertIsNone(pending_question(self.db, run_id=self.run.id))


if __name__ == "__main__":
    unittest.main()
