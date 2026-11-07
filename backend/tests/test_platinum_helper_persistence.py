"""Offline Platinum Helper persistence regression: no network, no paid API calls."""
import unittest
from random import Random

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from consts import QUESTION_CATEGORIES
from persistence.gameplay import GameConflict, record_answer
from persistence.game_routes import _game_info
from persistence.models import (
    Base, GameRun, WalletTransaction, PlatinumHelperCall, PlatinumHelperPurchase, Question,
    QuestionAttempt, StarAward, Topic, User,
)
from platinum_helper.core import CATEGORY_TOPICS, MAX_CONSULTATIONS, TOPICS
from platinum_helper.service import (
    available_platinum, buy, card, finish_call, get_purchase,
    refund_if_earned, reserve_call,
)


class PlatinumHelperPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False, autoflush=False)
        self.user = User(username="helper", username_key="helper")
        self.db.add(self.user)
        self.db.flush()
        self.run = GameRun(
            user_id=self.user.id, language="English", status="main",
            correct_main_answers=0, hints_used=0,
        )
        self.past = GameRun(
            user_id=self.user.id, language="English", status="completed",
            correct_main_answers=15, hints_used=0,
        )
        self.db.add_all([self.run, self.past])
        self.db.flush()
        self.db.add(StarAward(user_id=self.user.id, run_id=self.past.id, tier="platinum"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def purchase(self, profession=None):
        return buy(self.db, user_id=self.user.id, run_id=self.run.id,
                   profession=profession, name="Sam", tagline="Hi there",
                   rng=Random(51))

    def issue_question(self):
        topic = Topic(slug="test-history", labels={"en": "Modern history"})
        self.db.add(topic)
        self.db.flush()
        question = Question(
            topic_id=topic.id, language="English", requested_difficulty=0,
            text="Which year?", choices={"A": "100", "B": "200", "C": "300", "D": "400"},
            correct_key="C", fingerprint="f" * 64, status="approved",
            generator_provider="test", generator_model="test",
            prompt_version="test", schema_version="test",
        )
        self.db.add(question)
        self.db.flush()
        attempt = QuestionAttempt(
            run_id=self.run.id, question_id=question.id,
            number=1, requested_difficulty=0,
        )
        self.db.add(attempt)
        self.db.commit()
        return question, attempt

    def test_all_quiz_categories_have_competence_mapping(self):
        self.assertEqual(set(QUESTION_CATEGORIES), set(CATEGORY_TOPICS))
        self.assertTrue(set(CATEGORY_TOPICS.values()) <= set(TOPICS))

    def test_random_purchase_and_idempotent_retry(self):
        self.assertEqual(available_platinum(self.db, self.user.id), 1)
        first = self.purchase()
        self.assertEqual(first["price"], 1)
        self.assertEqual(first["consultations_remaining"], 3)
        self.assertEqual(available_platinum(self.db, self.user.id), 0)
        repeat = self.purchase()
        self.assertEqual(first, repeat)
        self.assertEqual(available_platinum(self.db, self.user.id), 0)

    def test_saved_unstarted_game_exposes_the_purchased_helper_without_extra_charge(self):
        purchased = self.purchase()
        self.db.expire_all()  # Model an HTTP request in a fresh session.
        run = self.db.get(GameRun, self.run.id)
        saved = _game_info(self.db, run)
        self.assertEqual(str(saved.game_id), str(run.id))
        self.assertEqual(saved.phase, "main")
        self.assertFalse(saved.question_issued)
        self.assertEqual(saved.main_question_index, 0)
        self.assertEqual(saved.helper, purchased)
        self.assertEqual(available_platinum(self.db, self.user.id), 0)

    def test_selected_profession_requires_two_platinum(self):
        with self.assertRaises(GameConflict):
            self.purchase("Physicist")
        self.assertEqual(available_platinum(self.db, self.user.id), 1)
        self.assertIsNone(get_purchase(self.db, self.run.id))

    def test_profile_is_private_and_unchanged_after_reload(self):
        self.purchase()
        p = get_purchase(self.db, self.run.id)
        original = dict(p.profile)
        public = card(p)
        self.assertNotIn("skills", public)
        self.assertNotIn("correct_answer", public)
        self.db.expire_all()
        self.assertEqual(get_purchase(self.db, self.run.id).profile, original)

    def test_refund_at_fifteen_even_when_bonus_lost(self):
        self.purchase()
        self.run.correct_main_answers = 15
        self.run.status = "bonus"
        self.assertTrue(refund_if_earned(self.db, self.run))
        self.db.commit()
        self.assertEqual(available_platinum(self.db, self.user.id), 1)
        self.run.status = "completed"
        self.run.bonus_correct = False
        self.assertFalse(refund_if_earned(self.db, self.run))
        self.db.commit()
        self.assertEqual(available_platinum(self.db, self.user.id), 1)

    def test_helper_use_forfeits_refund_but_does_not_change_hints(self):
        self.purchase()
        question, _ = self.issue_question()
        _, _, cached, payload = reserve_call(
            self.db, user_id=self.user.id, run_id=self.run.id,
            question_id=question.id, request_key="fixed-call-001", rng=Random(8),
        )
        self.assertFalse(cached)
        self.assertEqual(self.run.hints_used, 0)
        result = finish_call(
            self.db, user_id=self.user.id, run_id=self.run.id,
            call_id=payload["call_id"], reply="Some advice",
        )
        self.assertEqual(result["helper"]["consultations_remaining"], 2)
        self.assertEqual(self.run.hints_used, 0)
        self.run.correct_main_answers = 15
        self.run.status = "bonus"
        self.assertFalse(refund_if_earned(self.db, self.run))
        self.db.commit()
        self.assertEqual(available_platinum(self.db, self.user.id), 0)

    def test_failed_call_retries_same_outcome_without_charge(self):
        self.purchase()
        question, _ = self.issue_question()
        args = dict(user_id=self.user.id, run_id=self.run.id,
                    question_id=question.id, request_key="call-retry-123")
        _, _, _, first = reserve_call(self.db, **args, rng=Random(1))
        self.assertIsNone(finish_call(
            self.db, user_id=self.user.id, run_id=self.run.id,
            call_id=first["call_id"], reply=None,
        ))
        self.assertEqual(get_purchase(self.db, self.run.id).consultations_used, 0)
        _, _, _, repeated = reserve_call(self.db, **args, rng=Random(555))
        self.assertEqual(first["outcome"], repeated["outcome"])
        self.assertEqual(first["letter"], repeated["letter"])
        self.assertEqual(first["call_id"], repeated["call_id"])
        finish_call(self.db, user_id=self.user.id, run_id=self.run.id,
                    call_id=first["call_id"], reply="A valid response")
        old, _, cached, _ = reserve_call(self.db, **args)
        self.assertTrue(cached)
        self.assertEqual(old, "A valid response")
        self.assertEqual(get_purchase(self.db, self.run.id).consultations_used, 1)

    def test_real_q15_transition_refunds_platinum_and_opens_bonus(self):
        self.purchase()
        self.run.correct_main_answers = 14
        self.run.status = "main"
        topic = Topic(slug="final-topic", labels={"en": "History"})
        self.db.add(topic)
        self.db.flush()
        q = Question(topic_id=topic.id, language="English", requested_difficulty=14,
                     text="Final Q15?", choices={"A": "1", "B": "2", "C": "3", "D": "4"},
                     correct_key="C", fingerprint="a" * 64, status="approved",
                     generator_provider="test", generator_model="test",
                     prompt_version="test", schema_version="test")
        self.db.add(q)
        self.db.flush()
        self.db.add(QuestionAttempt(run_id=self.run.id, question_id=q.id,
                                    number=15, requested_difficulty=14))
        self.db.commit()
        result, _ = record_answer(self.db, run_id=self.run.id, user_id=self.user.id,
                                  question_id=q.id, selected="C")
        self.assertEqual(result.phase, "bonus")
        self.assertEqual(result.achievement, "gold")
        self.assertEqual(available_platinum(self.db, self.user.id), 1)
        self.assertEqual(get_purchase(self.db, self.run.id).consultations_used, 0)

    def test_calls_not_allowed_in_bonus(self):
        self.purchase()
        question, _ = self.issue_question()
        self.run.correct_main_answers = 15
        self.run.status = "bonus"
        self.db.commit()
        with self.assertRaises(GameConflict):
            reserve_call(self.db, user_id=self.user.id, run_id=self.run.id,
                         question_id=question.id, request_key="call-in-bonus")

    def test_cannot_buy_after_first_question(self):
        self.issue_question()
        with self.assertRaises(GameConflict):
            self.purchase()


if __name__ == "__main__":
    unittest.main()
