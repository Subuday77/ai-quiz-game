"""Offline Platinum Helper persistence regression: no network, no paid API calls."""
import json
import os
import unittest
from datetime import datetime, timezone
from random import Random
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from consts import QUESTION_CATEGORIES
from persistence.gameplay import GameConflict, record_answer
from persistence.game_routes import _game_info
from persistence.models import (
    Base, GameRun, WalletTransaction, PlatinumHelperCall, PlatinumHelperPurchase, Question,
    QuestionAttempt, QuestionReview, QuestionFact, StarAward, Topic, User,
)
from platinum_helper.core import CATEGORY_TOPICS, MAX_CONSULTATIONS, TOPICS
from platinum_helper.service import (
    available_platinum, buy, card, finish_call, get_purchase,
    refund_if_earned, reserve_call, _recent_speech_history,
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

    def test_reviewed_competence_overrides_game_category(self):
        self.purchase()
        question, _ = self.issue_question()
        topic = self.db.get(Topic, question.topic_id)
        topic.labels = {"en": "Inventions and discoveries"}
        question.competence_area = "Physics"
        purchase = get_purchase(self.db, self.run.id)
        profile = purchase.profile
        purchase.profile = {
            **profile,
            "skills": {**profile["skills"], "Physics": "HIGH", "Technology": "MEDIUM"},
        }
        self.db.commit()
        with patch("platinum_helper.service.debug_reserved") as trace:
            _, _, cached, payload = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="reviewed-physics", rng=Random(3),
            )
        self.assertFalse(cached)
        self.assertEqual(payload["topic"], "Physics")
        saved = self.db.get(PlatinumHelperCall, payload["call_id"])
        self.assertEqual(saved.topic, "Physics")
        self.assertEqual(trace.call_args.kwargs["competence_source"], "question")

    def test_old_german_induction_question_infers_physics_from_english_fact(self):
        self.purchase()
        question, _ = self.issue_question()
        self.db.get(Topic, question.topic_id).labels = {
            "en": "Inventions and discoveries"
        }
        question.language = "German"
        self.run.language = "German"
        question.text = ("Welcher Wissenschaftler wird allgemein mit der Entdeckung "
                         "der elektromagnetischen Induktion in Verbindung gebracht?")
        self.db.add(QuestionFact(
            question_id=question.id, fact_key="a" * 64,
            subject="electromagnetic-induction", relation="discoverer",
            object="michael-faraday",
        ))
        self.db.commit()
        self.assertIsNone(question.competence_area)
        with patch("platinum_helper.service.debug_reserved") as trace:
            _, _, _, payload = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="old-german-faraday", rng=Random(3),
            )
        self.assertEqual(payload["topic"], "Physics")
        self.assertEqual(trace.call_args.kwargs["competence_source"], "legacy_metadata")
        self.assertIsNone(question.competence_area)

    def test_old_question_without_specific_fact_uses_observable_category_fallback(self):
        self.purchase()
        question, _ = self.issue_question()
        self.db.get(Topic, question.topic_id).labels = {
            "en": "Inventions and discoveries"
        }
        self.db.commit()
        with patch("platinum_helper.service.debug_reserved") as trace:
            _, _, _, payload = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="old-no-metadata", rng=Random(3),
            )
        self.assertEqual(payload["topic"], "Technology")
        self.assertEqual(trace.call_args.kwargs["competence_source"], "legacy_category")

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
        self.assertEqual(first["delivery_style"], repeated["delivery_style"])
        self.assertEqual(first["presentation_style"], repeated["presentation_style"])
        self.assertEqual(first["call_id"], repeated["call_id"])
        finish_call(self.db, user_id=self.user.id, run_id=self.run.id,
                    call_id=first["call_id"], reply="A valid response")
        old, _, cached, _ = reserve_call(self.db, **args)
        self.assertTrue(cached)
        self.assertEqual(old, "A valid response")
        self.assertEqual(get_purchase(self.db, self.run.id).consultations_used, 1)


    def test_three_calls_reuse_anonymized_speech_after_reload_without_recharge(self):
        self.purchase()
        question, first_attempt = self.issue_question()
        with patch("platinum_helper.service.choose_outcome", return_value="KNOW"):
            _, _, cached, first = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="speech-memory-first",
            )
        self.assertFalse(cached)
        self.assertEqual(first["speech_history"], [])
        finish_call(self.db, user_id=self.user.id, run_id=self.run.id,
                    call_id=first["call_id"], reply="Мне кажется, тут 300.")
        first_attempt.answered_at = datetime.now(timezone.utc)
        self.db.commit()

        second_attempt = QuestionAttempt(
            run_id=self.run.id, question_id=question.id,
            number=2, requested_difficulty=1,
        )
        self.db.add(second_attempt)
        self.db.commit()
        # Mimic a browser reload with a fresh SQLAlchemy state.
        self.db.expire_all()
        with patch("platinum_helper.service.choose_outcome", return_value="KNOW"):
            _, _, cached, second = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="speech-memory-second",
            )
        self.assertFalse(cached)
        self.assertEqual(second["speech_history"],
                         ["Мне кажется, тут [ANSWER]."])
        self.assertNotIn("300", str(second["speech_history"]))
        finish_call(self.db, user_id=self.user.id, run_id=self.run.id,
                    call_id=second["call_id"], reply="По-моему, тут 300.")
        second_attempt.answered_at = datetime.now(timezone.utc)
        self.db.commit()

        third_attempt = QuestionAttempt(
            run_id=self.run.id, question_id=question.id,
            number=3, requested_difficulty=2,
        )
        self.db.add(third_attempt)
        self.db.commit()
        self.db.expire_all()
        with patch("platinum_helper.service.choose_outcome", return_value="KNOW"):
            _, _, cached, third = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="speech-memory-third",
            )
        self.assertFalse(cached)
        self.assertEqual(third["speech_history"], [
            "Мне кажется, тут [ANSWER].",
            "По-моему, тут [ANSWER].",
        ])
        result = finish_call(
            self.db, user_id=self.user.id, run_id=self.run.id,
            call_id=third["call_id"], reply="Я за 300.",
        )
        self.assertEqual(result["helper"]["consultations_remaining"], 0)
        old_text, helper, cached, payload = reserve_call(
            self.db, user_id=self.user.id, run_id=self.run.id,
            question_id=question.id, request_key="speech-memory-third",
        )
        self.assertTrue(cached)
        self.assertIsNone(payload)
        self.assertEqual(old_text, "Я за 300.")
        self.assertEqual(helper["consultations_remaining"], 0)

    def test_speech_history_excludes_failed_pending_and_other_hires(self):
        self.purchase()
        question, attempt = self.issue_question()
        own = get_purchase(self.db, self.run.id)
        other = PlatinumHelperPurchase(
            run_id=self.past.id, user_id=self.user.id,
            price=1, profession=own.profession, profile=dict(own.profile),
            consultations_used=1, refunded=False,
        )
        self.db.add(other)
        self.db.flush()
        past_attempt = QuestionAttempt(
            run_id=self.past.id, question_id=question.id, number=1,
            requested_difficulty=0,
        )
        self.db.add(past_attempt)
        self.db.flush()
        self.db.add_all([
            PlatinumHelperCall(
                purchase_id=own.id, request_key="speech-failed",
                question_attempt_id=attempt.id, topic="History",
                outcome="KNOW", selected_key="C", status="failed",
                response_text="Мне кажется, тут 300.",
            ),
            PlatinumHelperCall(
                purchase_id=own.id, request_key="speech-pending",
                question_attempt_id=attempt.id, topic="History",
                outcome="KNOW", selected_key="C", status="pending",
                response_text="По-моему, тут 300.",
            ),
            PlatinumHelperCall(
                purchase_id=other.id, request_key="another-game",
                question_attempt_id=past_attempt.id, topic="History",
                outcome="KNOW", selected_key="C", status="completed",
                response_text="Другой помощник выбрал 300.",
            ),
        ])
        self.db.commit()
        self.assertEqual(_recent_speech_history(self.db, own.id), [])
        self.assertEqual(_recent_speech_history(self.db, other.id),
                         ["Другой помощник выбрал [ANSWER]."])
        # A prior UNSURE reply has no selected label and remains style-only.
        self.db.add(PlatinumHelperCall(
            purchase_id=own.id, request_key="speech-unsure",
            question_attempt_id=attempt.id, topic="History",
            outcome="UNSURE", selected_key=None, status="completed",
            response_text="Не знаю, тут пас.",
        ))
        self.db.commit()
        self.assertEqual(_recent_speech_history(self.db, own.id),
                         ["Не знаю, тут пас."])

    def test_plausible_mistake_uses_saved_generator_scores(self):
        self.purchase()
        question, attempt = self.issue_question()
        self.db.add(QuestionReview(
            question_id=question.id, checker_type="generator_plausibility",
            provider="test", contract_version="generator_plausibility_v1",
            decision="unverified_estimate", details={
                "scores": {"A": 55, "B": 5, "C": 100, "D": 15},
            },
        ))
        self.db.commit()
        with patch("platinum_helper.service.choose_outcome", return_value="MISTAKEN"):
            _, _, _, payload = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="plausible-score-1", rng=Random(3),
            )
        self.assertEqual(payload["outcome"], "MISTAKEN")
        self.assertEqual(payload["letter"], "A")
        self.assertEqual(self.db.get(PlatinumHelperCall, payload["call_id"]).selected_key, "A")

    def test_implausible_mistake_abstains_and_retry_does_not_reroll(self):
        self.purchase()
        question, attempt = self.issue_question()
        self.db.add(QuestionReview(
            question_id=question.id, checker_type="generator_plausibility",
            provider="test", contract_version="generator_plausibility_v1",
            decision="unverified_estimate", details={
                "scores": {"A": 35, "B": 7, "C": 100, "D": 9},
            },
        ))
        self.db.commit()
        args = dict(user_id=self.user.id, run_id=self.run.id,
                    question_id=question.id, request_key="abstain-score-1")
        with patch("platinum_helper.service.choose_outcome", return_value="MISTAKEN"):
            _, _, _, first = reserve_call(self.db, **args, rng=Random(3))
        self.assertEqual(first["outcome"], "UNSURE")
        self.assertIsNone(first["letter"])
        self.assertIsNone(finish_call(self.db, user_id=self.user.id,
                                     run_id=self.run.id, call_id=first["call_id"],
                                     reply=None))
        with patch("platinum_helper.service.choose_outcome", return_value="KNOW"):
            _, _, _, retried = reserve_call(self.db, **args, rng=Random(4))
        self.assertEqual(retried["outcome"], "UNSURE")
        self.assertIsNone(retried["letter"])
        self.assertEqual(retried["call_id"], first["call_id"])
        self.assertEqual(get_purchase(self.db, self.run.id).consultations_used, 0)

    def test_platinum_mistake_ignores_bronze_hidden_options(self):
        self.purchase()
        question, attempt = self.issue_question()
        attempt.hints_used = 2
        self.db.add(QuestionReview(
            question_id=question.id, checker_type="generator_plausibility",
            provider="test", contract_version="generator_plausibility_v1",
            decision="unverified_estimate", details={
                "scores": {"A": 85, "B": 15, "C": 100, "D": 5},
            },
        ))
        self.db.commit()
        with patch("platinum_helper.service.choose_outcome", return_value="MISTAKEN"):
            _, _, _, payload = reserve_call(
                self.db, user_id=self.user.id, run_id=self.run.id,
                question_id=question.id, request_key="bronze-score-1", rng=Random(2),
            )
        self.assertEqual(payload["letter"], "A")
        self.assertEqual(payload["outcome"], "MISTAKEN")

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


    def test_opt_in_helper_debug_reports_all_skills_and_actual_selection(self):
        with patch.dict(os.environ, {"QUIZ_HELPER_DEBUG": "true"}):
            with self.assertLogs("quiz.helper.debug", level="DEBUG") as captured:
                public = self.purchase()
                question, _ = self.issue_question()
                _, _, cached, payload = reserve_call(
                    self.db, user_id=self.user.id, run_id=self.run.id,
                    question_id=question.id, request_key="qa-debug-0001",
                    rng=Random(8),
                )
                self.assertFalse(cached)
                result = finish_call(
                    self.db, user_id=self.user.id, run_id=self.run.id,
                    call_id=payload["call_id"], reply="Example validated speech",
                )
        records = [
            json.loads(line.split("HELPER_DEBUG: ", 1)[1])
            for line in captured.output
        ]
        self.assertEqual([r["event"] for r in records], [
            "helper_profile_purchased",
            "helper_consultation_reserved",
            "helper_consultation_finished",
        ])
        created, reserved, finished = records
        self.assertNotIn("skills", public)  # Private data never enters HTTP card.
        for row in (created, reserved):
            self.assertEqual(set(row["skills"]), {"HIGH", "MEDIUM", "LOW"})
            self.assertEqual(
                set(sum(row["skills"].values(), [])), set(TOPICS),
            )
        self.assertEqual(reserved["question_category"], "Modern history")
        self.assertEqual(reserved["competence_area"], "History")
        self.assertEqual(
            reserved["competence_level"],
            get_purchase(self.db, self.run.id).profile["skills"]["History"],
        )
        self.assertEqual(
            reserved["outcome_probabilities_percent"],
            {"KNOW": 90, "MISTAKEN": 8, "UNSURE": 2}
            if reserved["competence_level"] == "HIGH"
            else ({"KNOW": 45, "MISTAKEN": 30, "UNSURE": 25}
                  if reserved["competence_level"] == "MEDIUM"
                  else {"KNOW": 5, "MISTAKEN": 15, "UNSURE": 80}),
        )
        correct = (
            reserved["selected_key"] == question.correct_key
            if reserved["selected_key"] is not None else None
        )
        self.assertEqual(reserved["selected_answer_correct"], correct)
        self.assertEqual(finished["selected_answer_correct"], correct)
        self.assertEqual(finished["confidence"], reserved["confidence"])
        if reserved["selected_key"] is not None:
            self.assertIn(reserved["confidence"], ("CONFIDENT", "TENTATIVE"))
        else:
            self.assertIsNone(reserved["confidence"])
        self.assertTrue(finished["charged"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["speech"], "Example validated speech")
        self.assertEqual(result["text"], "Example validated speech")

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
