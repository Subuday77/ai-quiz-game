"""SQLite regression of the optional Q16 gold wager and immutable awards."""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from persistence.bonus_wager import accept_bonus, decline_bonus
from persistence.gameplay import GameConflict, record_answer
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, StarAward, Topic, User,
)
from persistence.star_economy import star_balance


class BonusWagerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.user = User(username="bonus", username_key="bonus")
        self.db.add(self.user)
        self.db.flush()
        self.run = GameRun(user_id=self.user.id, language="Russian", status="bonus",
                           correct_main_answers=15, hints_used=0)
        self.db.add(self.run)
        self.db.flush()
        self.db.add(StarAward(user_id=self.user.id, run_id=self.run.id, tier="gold"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def issue_q16(self):
        topic = Topic(slug="bonus-test", labels={"en": "History"})
        self.db.add(topic)
        self.db.flush()
        question = Question(
            topic_id=topic.id, language="Russian", requested_difficulty=15,
            text="Bonus?", choices={"A":"1","B":"2","C":"3","D":"4"},
            correct_key="C", fingerprint="d" * 64, status="approved",
            generator_provider="test", generator_model="test",
            prompt_version="test", schema_version="test",
        )
        self.db.add(question)
        self.db.flush()
        self.db.add(QuestionAttempt(run_id=self.run.id, question_id=question.id,
                                    number=16, requested_difficulty=15))
        self.db.commit()
        return question

    def test_accept_stakes_once_and_win_restores_gold(self):
        self.assertEqual(star_balance(self.db, self.user.id, "gold"), 1)
        accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)
        accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)
        self.assertTrue(self.run.bonus_wager_accepted)
        self.assertEqual(star_balance(self.db, self.user.id, "gold"), 0)
        question = self.issue_q16()
        result, _ = record_answer(
            self.db, run_id=self.run.id, user_id=self.user.id,
            question_id=question.id, selected="C",
        )
        self.assertTrue(result.correct)
        self.assertEqual(result.achievement, "platinum")
        self.assertEqual(star_balance(self.db, self.user.id, "gold"), 1)
        self.assertEqual(star_balance(self.db, self.user.id, "platinum"), 1)

    def test_loss_forfeits_gold_but_not_its_earned_award(self):
        accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)
        question = self.issue_q16()
        result, _ = record_answer(
            self.db, run_id=self.run.id, user_id=self.user.id,
            question_id=question.id, selected="A",
        )
        self.assertFalse(result.correct)
        self.assertEqual(star_balance(self.db, self.user.id, "gold"), 0)
        self.assertEqual(star_balance(self.db, self.user.id, "platinum"), 0)
        self.assertEqual(self.db.query(StarAward).filter_by(tier="gold").count(), 1)

    def test_decline_keeps_gold_without_issuing_q16(self):
        done = decline_bonus(self.db, user_id=self.user.id, run_id=self.run.id)
        self.assertEqual(done.status, "completed")
        self.assertEqual(star_balance(self.db, self.user.id, "gold"), 1)
        with self.assertRaises(GameConflict):
            accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)

    def test_no_wager_without_gold_or_after_question_shown(self):
        self.db.query(StarAward).delete()
        self.db.commit()
        with self.assertRaises(GameConflict):
            accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)
        self.db.add(StarAward(user_id=self.user.id, run_id=self.run.id, tier="gold"))
        self.db.commit()
        self.issue_q16()
        with self.assertRaises(GameConflict):
            accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)

    def test_can_not_decline_after_staking(self):
        accept_bonus(self.db, user_id=self.user.id, run_id=self.run.id)
        with self.assertRaises(GameConflict):
            decline_bonus(self.db, user_id=self.user.id, run_id=self.run.id)


if __name__ == "__main__":
    unittest.main()
