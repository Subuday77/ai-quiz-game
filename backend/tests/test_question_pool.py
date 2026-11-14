"""Offline global-question-pool and strict emergency reserve regressions."""
import os
import unittest
from copy import deepcopy
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.game_routes import _category_for_round, _main_pool_search_categories
from persistence.gameplay import create_game, offer_question
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, QuestionReview, Topic, WalletTransaction,
)
from persistence.question_pool import (
    enabled, offer_approved_pool_question, scan_limit,
)
from persistence.seed_topics import seed_topics
from tagging.review import is_strict_too_easy_reserve
from test_tagged_dedup import candidate


def review_report():
    return {
        "checker_type": "ai_tagged_critic",
        "provider": "openai",
        "model": "test",
        "contract_version": "tagged_review_v5",
        "decision": "accept",
        "issues": [],
        "details": {"assessment": {"total": 0}, "verdict": {"facts": "pass"}},
        "duration_ms": 1,
    }


def too_easy_report():
    return {
        **review_report(),
        "decision": "reject",
        "issues": ["difficulty_mismatch"],
        "details": {
            "verdict": {
                "difficulty": "too_easy",
                "issue_codes": ["difficulty_mismatch"],
                "facts": "pass", "options": "pass", "language": "pass",
                "leakage": "pass", "tags_valid": "pass", "fact_valid": "pass",
                "semantic_duplicate": False, "premise": "valid",
            },
            "assessment": {
                "critical": "none",
                "raw_penalties": {
                    "distractors": 0, "knowledge": 0, "suitability": 0, "wording": 0,
                },
            },
            "option_diagnostics": {
                "outcome": "single_correct",
                "supported_choices": ["B"],
                "uncertain_choices": [],
            },
            "safeguard_codes": [],
        },
    }


class StrictReserveTests(unittest.TestCase):
    def test_only_too_easy_and_no_other_penalty_is_eligible(self):
        q = candidate()
        q._review_report = too_easy_report()
        self.assertTrue(is_strict_too_easy_reserve(q))
        for key in ("distractors", "knowledge", "suitability", "wording"):
            with self.subTest(penalty=key):
                bad = q.model_copy(deep=True)
                bad._review_report["details"]["assessment"]["raw_penalties"][key] = 1
                self.assertFalse(is_strict_too_easy_reserve(bad))
        for path, value in [
            (("verdict", "facts"), "fail"),
            (("verdict", "options"), "uncertain"),
            (("verdict", "tags_valid"), "fail"),
            (("verdict", "semantic_duplicate"), True),
            (("assessment", "critical"), "regenerate"),
            (("option_diagnostics", "outcome"), "multiple_correct"),
        ]:
            with self.subTest(path=path):
                bad = q.model_copy(deep=True)
                bad._review_report["details"][path[0]][path[1]] = value
                self.assertFalse(is_strict_too_easy_reserve(bad))
        bad = q.model_copy(deep=True)
        bad._review_report["details"]["verdict"]["issue_codes"].append("bad_distractors")
        self.assertFalse(is_strict_too_easy_reserve(bad))
        bad = q.model_copy(deep=True)
        bad._review_report["decision"] = "accept"
        self.assertFalse(is_strict_too_easy_reserve(bad))


class PoolConfigTests(unittest.TestCase):
    def test_flag_and_scan_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(enabled("QUIZ_QUESTION_POOL_FALLBACK_ENABLED"))
            self.assertEqual(scan_limit(), 100)
        for v in ("0", "maybe", ""):
            with patch.dict(os.environ, {"QUIZ_QUESTION_POOL_FALLBACK_ENABLED": v}):
                with self.assertRaises(ValueError):
                    enabled("QUIZ_QUESTION_POOL_FALLBACK_ENABLED")
        for v in ("0", "1001", "nan", "2.5"):
            with patch.dict(os.environ, {"QUIZ_QUESTION_POOL_SCAN_LIMIT": v}):
                with self.assertRaises(ValueError):
                    scan_limit()


class GlobalPoolTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as db:
            seed_topics(db)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.enterContext(patch.dict(os.environ, {
            "AUTH_COOKIE_SECURE": "false",
            "AI_TAG_DEDUP_ENABLED": "true",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "true",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "true",
            "QUIZ_QUESTION_POOL_SCAN_LIMIT": "100",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.alice = self._register(self.client, "Alice")
        response = self.client.post("/api/v2/games", json={"language": "English"})
        self.assertEqual(response.status_code, 201, response.text)
        self.run_id = UUID(response.json()["game_id"])
        self.category = _category_for_round(self.run_id, 1)
        with TestClient(app) as other:
            self.bob = self._register(other, "Bob")

    def _register(self, client, name):
        response = client.post("/api/auth/register", json={
            "username": name, "password": "offline-test-password-2026!",
        })
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["id"])

    def seed_other_user_question(self, *, accepted=True, category=None,
                                 question=None):
        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            bob_run = create_game(db, user_id=self.bob, language="English")
            q = question if question is not None else candidate()
            if accepted:
                q._review_report = review_report()
            attempt, saved = offer_question(
                db, run_id=bob_run.id, user_id=self.bob, question=q,
                category=category or self.category, provider="test", model="stub",
                dedup_enforce=True,
            )
            return saved.id

    def request(self):
        return self.client.get(f"/api/v2/games/{self.run_id}/question")

    def _silver_credit(self, amount):
        with Session(self.engine) as db:
            db.add(WalletTransaction(
                user_id=self.alice, reason="star/silver/offline-test-credit",
                amount=amount, idempotency_key=str(uuid4()),
            ))
            db.commit()

    def _silver_balance(self):
        return self.client.get("/api/v2/stars").json()["silver"]

    def test_pool_first_q1_searches_other_allowed_categories_before_ai(self):
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            search = _main_pool_search_categories(db, run, 1, self.category)
            self.assertEqual(search[0], self.category)
            self.assertEqual(set(search), set(run.allowed_categories or __import__(
                "consts").QUESTION_CATEGORIES))
            secondary = search[-1]  # Force an exhaustive search, not one backup.
        pooled_id = self.seed_other_user_question(category=secondary)
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as ai:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(pooled_id))
        ai.assert_not_called()
        with Session(self.engine) as db:
            stored = db.get(Question, pooled_id)
            self.assertEqual(db.get(Topic, stored.topic_id).labels["en"], secondary)

    def test_db_first_queries_all_categories_once_and_keeps_order(self):
        """A late-category hit uses one ranked SQL candidate scan, not N scans."""
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            categories = _main_pool_search_categories(db, run, 1, self.category)
            self.assertGreater(len(categories), 10)
            late_category = categories[-1]
        pooled = self.seed_other_user_question(category=late_category)
        ranked_queries = []
        candidate_queries = []

        def observe(conn, cursor, statement, parameters, context, many):
            sql = " ".join(statement.lower().split())
            if ("from questions join topics" in sql
                    and "question_reviews" in sql and "question_facts" in sql):
                candidate_queries.append(statement)
            if "row_number(" in sql and "questions" in sql:
                ranked_queries.append(statement)

        event.listen(self.engine, "before_cursor_execute", observe)
        try:
            with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
                "persistence.question_pool.random.random", return_value=0.0
            ), patch("api.generate_valid_question") as ai:
                response = self.request()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["question_id"], str(pooled))
            self.assertEqual(len(ranked_queries), 1)
            self.assertEqual(len(candidate_queries), 1)
            ai.assert_not_called()
        finally:
            event.remove(self.engine, "before_cursor_execute", observe)

    def test_db_first_prefers_primary_admission_over_later_category(self):
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            categories = _main_pool_search_categories(db, run, 1, self.category)
            secondary = categories[-1]
        primary_id = self.seed_other_user_question(category=self.category)
        secondary_id = self.seed_other_user_question(
            category=secondary,
            question=candidate(relation="nobel-prize-reason",
                               concept="photoelectric-effect",
                               answer="photoelectric-effect"),
        )
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as ai:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(primary_id))
        self.assertNotEqual(response.json()["question_id"], str(secondary_id))
        ai.assert_not_called()

    def test_db_first_moves_to_later_category_on_rejected_rating(self):
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            categories = _main_pool_search_categories(db, run, 1, self.category)
            secondary = categories[-1]
        primary_id = self.seed_other_user_question(category=self.category)
        secondary_id = self.seed_other_user_question(
            category=secondary,
            question=candidate(relation="nobel-prize-reason",
                               concept="photoelectric-effect",
                               answer="photoelectric-effect"),
        )
        with Session(self.engine) as db:
            db.get(Question, primary_id).working_vote_score = -2
            db.get(Question, secondary_id).working_vote_score = 7
            db.commit()
        # Sigmoid(-2) ~0.45 rejects 0.6; sigmoid(7) ~0.67 admits it.
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.6
        ), patch("api.generate_valid_question") as ai:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(secondary_id))
        ai.assert_not_called()

    def test_pool_first_skips_paid_excluded_category(self):
        self._silver_credit(5)
        excluded = self.category
        response = self.client.post(
            f"/api/v2/games/{self.run_id}/categories/exclude",
            json={"categories": [excluded], "request_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            assigned = _category_for_round(run.id, 1, db=db)
            search = _main_pool_search_categories(db, run, 1, assigned)
            self.assertNotIn(excluded, search)
            other = search[-1]
        blocked_id = self.seed_other_user_question(category=excluded)
        eligible_id = self.seed_other_user_question(
            category=other,
            question=candidate(relation="nobel-prize-reason",
                               concept="photoelectric-effect",
                               answer="photoelectric-effect"),
        )
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as ai:
            result = self.request()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["question_id"], str(eligible_id))
        self.assertNotEqual(result.json()["question_id"], str(blocked_id))
        self.assertEqual(self._silver_balance(), 0)
        ai.assert_not_called()

    def test_pool_first_does_not_revisit_answered_category_on_q2(self):
        first_candidate = candidate()
        first_candidate._review_report = review_report()
        with patch("api.generate_valid_question", return_value=first_candidate):
            first = self.request()
        self.assertEqual(first.status_code, 200, first.text)
        with Session(self.engine) as db:
            generated = db.get(Question, UUID(first.json()["question_id"]))
            correct_text = generated.choices[generated.correct_key]
        visible_correct = next(k for k, value in first.json()["answers"].items()
                               if value == correct_text)
        answer = self.client.post(f"/api/v2/games/{self.run_id}/answer", json={
            "question_id": first.json()["question_id"], "answer": visible_correct,
        })
        self.assertEqual(answer.status_code, 200, answer.text)
        with Session(self.engine) as db:
            first_stored = db.get(Question, UUID(first.json()["question_id"]))
            used = db.get(Topic, first_stored.topic_id).labels["en"]
            run = db.get(GameRun, self.run_id)
            self.assertEqual(run.correct_main_answers, 1)
            primary = _category_for_round(self.run_id, 2, db=db)
            self.assertNotIn(used, _main_pool_search_categories(db, run, 2, primary))
            bob_run = create_game(db, user_id=self.bob, language="English")
            bob_run.correct_main_answers = 1
            db.commit()
            other_question = candidate(
                difficulty=1, relation="nobel-prize-reason",
                concept="photoelectric-effect", answer="photoelectric-effect",
            )
            other_question._review_report = review_report()
            offer_question(
                db, run_id=bob_run.id, user_id=self.bob, question=other_question,
                category=used, provider="test", model="stub", dedup_enforce=True,
            )
        generated = candidate(
            difficulty=1, relation="nobel-prize-reason",
            concept="photoelectric-effect", answer="photoelectric-effect",
        )
        generated._review_report = review_report()
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question", return_value=generated) as ai:
            second = self.request()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["number"], 2)
        ai.assert_called_once()
        self.assertNotEqual(ai.call_args.args[2], used)
        with Session(self.engine) as db:
            second_stored = db.get(Question, UUID(second.json()["question_id"]))
            self.assertNotEqual(db.get(Topic, second_stored.topic_id).labels["en"], used)

    def test_pool_first_main_q14_uses_other_topic_before_any_ai(self):
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            run.correct_main_answers = 13
            db.commit()
            primary = _category_for_round(run.id, 14, db=db)
            secondary = _main_pool_search_categories(db, run, 14, primary)[-1]
            bob_run = create_game(db, user_id=self.bob, language="English")
            bob_run.correct_main_answers = 13
            db.commit()
            item = candidate(difficulty=13)
            item._review_report = review_report()
            _, stored = offer_question(
                db, run_id=bob_run.id, user_id=self.bob, question=item,
                category=secondary, provider="test", model="stub",
                dedup_enforce=True,
            )
            expected_id = stored.id
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as ai:
            result = self.request()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["number"], 14)
        self.assertEqual(result.json()["difficulty"], 13)
        self.assertEqual(result.json()["question_id"], str(expected_id))
        ai.assert_not_called()

    def test_pool_first_reissue_after_quarantine_increments_revision(self):
        original = candidate()
        original._review_report = review_report()
        with patch("api.generate_valid_question", return_value=original):
            first = self.request()
        self.assertEqual(first.status_code, 200, first.text)
        with Session(self.engine) as db:
            old = db.get(Question, UUID(first.json()["question_id"]))
            old.status = "quarantined"
            run = db.get(GameRun, self.run_id)
            primary = _category_for_round(self.run_id, 1, db=db)
            alternative = _main_pool_search_categories(db, run, 1, primary)[-1]
            db.commit()
        pool_id = self.seed_other_user_question(
            category=alternative,
            question=candidate(relation="nobel-prize-reason",
                               concept="photoelectric-effect",
                               answer="photoelectric-effect"),
        )
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as ai:
            issued = self.request()
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.json()["question_id"], str(pool_id))
        ai.assert_not_called()
        with Session(self.engine) as db:
            revisions = db.scalars(select(QuestionAttempt).where(
                QuestionAttempt.run_id == self.run_id
            ).order_by(QuestionAttempt.revision)).all()
            self.assertEqual([x.revision for x in revisions], [0, 1])
            self.assertIsNotNone(revisions[0].superseded_at)
            self.assertIsNone(revisions[1].superseded_at)

    def test_paid_manual_pool_hit_keeps_exact_category_and_charges_once(self):
        original = candidate()
        original._review_report = review_report()
        with patch("api.generate_valid_question", return_value=original):
            first = self.request().json()
        self._silver_credit(2)
        base = f"/api/v2/games/{self.run_id}"
        selected = self.client.get(base + "/categories").json()["alternatives"][0]
        reused_id = self.seed_other_user_question(
            category=selected,
            question=candidate(relation="nobel-prize-reason",
                               concept="photoelectric-effect",
                               answer="photoelectric-effect"),
        )
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as ai:
            changed = self.client.post(base + "/categories/change", json={
                "question_id": first["question_id"], "mode": "manual",
                "category": selected, "request_id": str(uuid4()),
            })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["question_id"], str(reused_id))
        ai.assert_not_called()
        self.assertEqual(self._silver_balance(), 0)
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            attempts = db.scalars(select(QuestionAttempt).where(
                QuestionAttempt.run_id == run.id).order_by(QuestionAttempt.revision)).all()
            self.assertEqual(len(attempts), 2)
            self.assertIsNotNone(attempts[0].superseded_at)
            self.assertEqual(attempts[1].revision, 1)
            self.assertEqual(attempts[1].manual_category_used, 1)
            self.assertEqual(run.hints_used, 1)
            self.assertEqual(db.scalar(select(func.count(Question.id))), 2)
        self.assertEqual(self.request().json(), changed.json())

    def test_paid_manual_pool_miss_generates_only_selected_category(self):
        original = candidate()
        original._review_report = review_report()
        with patch("api.generate_valid_question", return_value=original):
            first = self.request().json()
        self._silver_credit(2)
        base = f"/api/v2/games/{self.run_id}"
        options = self.client.get(base + "/categories").json()["alternatives"]
        selected, other = options[:2]
        self.seed_other_user_question(
            category=other,
            question=candidate(relation="nobel-prize-reason",
                               concept="photoelectric-effect",
                               answer="photoelectric-effect"),
        )
        proposed = candidate(relation="nobel-prize-reason",
                             concept="photoelectric-effect", answer="photoelectric-effect")
        proposed._review_report = review_report()
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question", return_value=proposed) as ai:
            changed = self.client.post(base + "/categories/change", json={
                "question_id": first["question_id"], "mode": "manual",
                "category": selected, "request_id": str(uuid4()),
            })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(ai.call_args.args[2], selected)
        with Session(self.engine) as db:
            new = db.get(Question, UUID(changed.json()["question_id"]))
            self.assertEqual(db.get(Topic, new.topic_id).labels["en"], selected)
        self.assertEqual(self._silver_balance(), 0)

    def test_paid_manual_pool_miss_ai_failure_never_charges(self):
        original = candidate()
        original._review_report = review_report()
        with patch("api.generate_valid_question", return_value=original):
            first = self.request().json()
        self._silver_credit(2)
        base = f"/api/v2/games/{self.run_id}"
        selected = self.client.get(base + "/categories").json()["alternatives"][0]
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "api.generate_valid_question",
            side_effect=HTTPException(status_code=503, detail="AI unavailable"),
        ) as ai:
            result = self.client.post(base + "/categories/change", json={
                "question_id": first["question_id"], "mode": "manual",
                "category": selected, "request_id": str(uuid4()),
            })
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(ai.call_args.args[2], selected)
        self.assertEqual(self._silver_balance(), 2)
        self.assertEqual(self.request().json(), first)

    def test_emergency_reuses_other_users_question_without_ai_or_copy(self):
        existing_id = self.seed_other_user_question()
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )) as gen:
            response = self.request()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["question_id"], str(existing_id))
            self.assertNotIn("correct_answer", response.json())
            self.assertNotIn("fact", response.json())
            self.assertEqual(self.request().json(), response.json())
            gen.assert_called_once()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionReview.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_pool_first_uses_approved_question_without_model_call(self):
        existing_id = self.seed_other_user_question()
        with patch.dict(os.environ, {
            "QUIZ_DB_FIRST_GENERATION": "true",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "false",
        }), patch("persistence.question_pool.random.random", return_value=0.0), patch(
            "api.generate_valid_question"
        ) as generate:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(existing_id))
        generate.assert_not_called()
        self.assertNotIn("correct_answer", response.json())

    def test_reused_question_has_new_persisted_order_and_correct_visible_key(self):
        """A reused question cannot carry the previous player's answer letters."""
        from persistence.choice_order import display_choices, display_key
        existing_id = self.seed_other_user_question()
        with Session(self.engine) as db:
            original = db.get(Question, existing_id)
            previous = db.scalar(select(QuestionAttempt)
                .where(QuestionAttempt.question_id == existing_id))
            bob_options = display_choices(previous.choice_order, original.choices)
            true_answer = original.choices[original.correct_key]
            original_order = previous.choice_order
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ), patch("api.generate_valid_question") as generate:
            served = self.request()
        self.assertEqual(served.status_code, 200, served.text)
        shown = served.json()
        self.assertEqual(shown["question_id"], str(existing_id))
        self.assertNotEqual(shown["answers"], bob_options)
        self.assertEqual(set(shown["answers"].values()), set(bob_options.values()))
        self.assertEqual(shown, self.request().json())  # F5/retry invariant
        with Session(self.engine) as db:
            latest = db.scalar(select(QuestionAttempt)
                .where(QuestionAttempt.run_id == self.run_id))
            self.assertNotEqual(latest.choice_order, original_order)
            self.assertEqual(display_choices(latest.choice_order,
                                             db.get(Question, existing_id).choices),
                             shown["answers"])
            visible_correct = display_key(latest.choice_order, "B")
        self.assertEqual(shown["answers"][visible_correct], true_answer)
        answered = self.client.post(f"/api/v2/games/{self.run_id}/answer", json={
            "question_id": shown["question_id"], "answer": visible_correct,
            "vote": -1,
        })
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertTrue(answered.json()["correct"])
        self.assertEqual(answered.json()["correct_answer"], visible_correct)
        with Session(self.engine) as db:
            issued = db.scalar(select(QuestionAttempt)
                .where(QuestionAttempt.run_id == self.run_id))
            self.assertEqual(issued.selected_key, visible_correct)
            self.assertTrue(issued.is_correct)
        generate.assert_not_called()

    def test_shuffled_hints_and_appeal_snapshot_show_player_letters(self):
        from persistence.choice_order import display_choices, display_key
        existing_id = self.seed_other_user_question()
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.0
        ):
            served = self.request()
        self.assertEqual(served.status_code, 200, served.text)
        with Session(self.engine) as db:
            attempt = db.scalar(select(QuestionAttempt)
                .where(QuestionAttempt.run_id == self.run_id))
            question = db.get(Question, existing_id)
            expected_all = display_choices(attempt.choice_order, question.choices)
            visible_correct = display_key(attempt.choice_order, question.correct_key)
            attempt.hints_used = 1
            db.commit()

        recovered = self.request()
        self.assertEqual(recovered.status_code, 200, recovered.text)
        shown = recovered.json()
        self.assertEqual(len(shown["answers"]), 3)
        self.assertEqual(shown["answers"][visible_correct], expected_all[visible_correct])
        removed = set("ABCD") - set(shown["answers"])
        self.assertEqual(len(removed), 1)
        self.assertNotIn(visible_correct, removed)
        quote = self.client.get(
            f"/api/v2/games/{self.run_id}/appeals/{existing_id}/quote"
        )
        self.assertEqual(quote.status_code, 200, quote.text)
        snapshot = quote.json()["question_snapshot"]
        self.assertEqual(snapshot["answers"], expected_all)
        self.assertEqual(set(snapshot["hidden_keys"]), removed)

        wrong_key = next(key for key in shown["answers"] if key != visible_correct)
        lost = self.client.post(f"/api/v2/games/{self.run_id}/answer", json={
            "question_id": str(existing_id), "answer": wrong_key,
        })
        self.assertEqual(lost.status_code, 200, lost.text)
        self.assertFalse(lost.json()["correct"])
        self.assertEqual(lost.json()["correct_answer"], visible_correct)
        self.assertEqual(lost.json()["phase"], "lost")
        replay = self.client.get(
            f"/api/v2/games/{self.run_id}/appeals/lost-question"
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["answers"], shown["answers"])

    def test_pool_inspection_uses_original_q1_category_after_game_is_lost(self):
        """Regression: a completed Q1 moves the live category rotation."""
        from persistence.gameplay import record_answer
        from scripts.inspect_question_pool import inspect
        from persistence.models import GameRun

        existing_id = self.seed_other_user_question()
        with patch.dict(os.environ, {
            "QUIZ_DB_FIRST_GENERATION": "true",
        }), patch("persistence.question_pool.random.random", return_value=0.0):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(existing_id))
        with Session(self.engine) as db:
            from persistence.choice_order import display_key
            attempt = db.scalar(select(QuestionAttempt).where(
                QuestionAttempt.run_id == self.run_id,
                QuestionAttempt.question_id == existing_id,
            ))
            question = db.get(Question, existing_id)
            wrong_canonical = next(
                key for key in "ABCD" if key != question.correct_key
            )
            wrong_visible = display_key(attempt.choice_order, wrong_canonical)
            self.assertNotEqual(
                wrong_visible, display_key(attempt.choice_order, question.correct_key)
            )
            result, _ = record_answer(
                db, run_id=self.run_id, user_id=self.alice,
                question_id=existing_id, selected=wrong_visible,
            )
            self.assertEqual(db.get(GameRun, self.run_id).status, "lost")
        with Session(self.engine) as db:
            report = inspect(db, self.run_id, 1)
            self.assertEqual(report["initial_q1_category"], self.category)
            self.assertEqual(report["first_issued_category"], self.category)
            self.assertTrue(report["initial_category_matches_first_issued"])
            self.assertEqual(report["category_used_for_filter_counts"], self.category)
            self.assertNotEqual(report["selected_category_now"], self.category)
            self.assertEqual(report["category_basis"],
                             "pre-issuance seeded Q1 rotation")
            self.assertGreaterEqual(
                next(x["count"] for x in report["cumulative_sql_filter_counts"]
                     if x["stage"] == "same_category"), 1,
            )
            # Exact same row is "seen" today, but was not "seen" before
            # the issue timestamp. Prove the inspector does not confuse them.
            from datetime import datetime, timedelta, timezone
            row = report["matching_candidate_timeline"][0]
            shown = datetime.fromisoformat(row["first_shown_to_this_user_at"])
            if shown.tzinfo is None:
                shown = shown.replace(tzinfo=timezone.utc)
            before = inspect(db, self.run_id, 1, at=shown - timedelta(seconds=1))
            after = inspect(db, self.run_id, 1, at=shown + timedelta(seconds=1))
            self.assertFalse(before["matching_candidate_timeline"][0][
                "already_shown_to_user_at_requested_time"])
            self.assertTrue(after["matching_candidate_timeline"][0][
                "already_shown_to_user_at_requested_time"])
            self.assertEqual(before["category_used_for_filter_counts"], self.category)
            db.rollback()

    def test_pool_first_generates_when_verified_pool_is_empty(self):
        proposed = candidate()
        proposed._review_report = review_report()
        with patch.dict(os.environ, {
            "QUIZ_QUESTION_SOURCE_ORDER": "pool_first",
        }), patch("api.generate_valid_question", return_value=proposed) as generate:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question"], proposed.question)
        generate.assert_called_once()

    def test_db_first_probabilistic_filter_favors_but_never_guarantees_high_rating(self):
        older_id = self.seed_other_user_question()
        with TestClient(app) as third:
            charlie = self._register(third, "Charlie")
        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            charlie_run = create_game(db, user_id=charlie, language="English")
            new = candidate(
                relation="nobel-prize-reason",
                concept="photoelectric-effect",
                answer="photoelectric-effect",
            )
            new._review_report = review_report()
            _, high = offer_question(
                db, run_id=charlie_run.id, user_id=charlie,
                question=new, category=self.category,
                provider="test", model="stub", dedup_enforce=True,
            )
            db.get(Question, older_id).working_vote_score = -2
            high.working_vote_score = 7
            db.commit()
            chosen_id = high.id
        with patch("persistence.question_pool.random.random", return_value=0.6):
            with Session(self.engine) as db:
                result = offer_approved_pool_question(
                    db, run_id=self.run_id, user_id=self.alice,
                    category=self.category, rating_filter=True,
                )
                self.assertEqual(result[1].id, chosen_id)

    def test_db_first_zero_admissions_falls_back_to_ai(self):
        self.seed_other_user_question()
        generated = candidate(relation="nobel-prize-reason",
                              concept="photoelectric-effect",
                              answer="photoelectric-effect")
        generated._review_report = review_report()
        with patch.dict(os.environ, {"QUIZ_DB_FIRST_GENERATION": "true"}), patch(
            "persistence.question_pool.random.random", return_value=0.999999
        ), patch("api.generate_valid_question", return_value=generated) as create:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question"], generated.question)
        create.assert_called_once()

    def test_pool_excludes_queued_community_review(self):
        existing_id = self.seed_other_user_question()
        from persistence.models import QuestionVoteReview
        with Session(self.engine) as db:
            db.add(QuestionVoteReview(
                question_id=existing_id, referral_number=1, score_at_trigger=-15,
                status="pending",
            ))
            db.commit()
        with Session(self.engine) as db:
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            ))

    def test_pool_does_not_reissue_to_same_user(self):
        existing_id = self.seed_other_user_question()
        with Session(self.engine) as db:
            attempt, saved = offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            )
            self.assertEqual(saved.id, existing_id)
            self.assertIsNotNone(attempt)
        # A fresh Alice game at the same difficulty/category must not get
        # the already SHOWN fact, even though Alice has not answered it.
        with Session(self.engine) as db:
            second_run = create_game(db, user_id=self.alice, language="English")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=second_run.id, user_id=self.alice,
                category=self.category,
            ))
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_semantically_same_fact_in_other_language_is_not_reissued(self):
        # Exact-question exclusion alone would miss this: Alice saw the fact
        # in German, Bob's approved reusable question is in English.
        self.seed_other_user_question()
        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            german_run = create_game(db, user_id=self.alice, language="German")
            offer_question(
                db, run_id=german_run.id, user_id=self.alice,
                question=candidate(language="German"),
                category=self.category, provider="test", model="stub",
                dedup_enforce=True,
            )
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            ))
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_pool_requires_exact_language_category_and_difficulty(self):
        self.seed_other_user_question()
        with Session(self.engine) as db:
            german_run = create_game(db, user_id=self.alice, language="German")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=german_run.id, user_id=self.alice,
                category=self.category,
            ))
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category="A category with no matching question",
            ))
            from persistence.models import GameRun
            alice_run = db.get(GameRun, self.run_id)
            alice_run.correct_main_answers = 1
            db.commit()
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            ))

    def test_known_false_approved_fact_is_blocked_before_quarantine(self):
        # A historical critic accepted the false Hopper/Turing 1972 item.
        # It must not be served even if the operator has not run the
        # one-time DB quarantine yet.
        from test_fact_quarantine import WRONG
        from tagging.contract import TaggedQuestion
        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            bob_run = create_game(db, user_id=self.bob, language="English")
            wrong = TaggedQuestion.model_validate({**WRONG, "difficulty": 0})
            wrong._review_report = review_report()
            _, stored = offer_question(
                db, run_id=bob_run.id, user_id=self.bob,
                question=wrong, category=self.category,
                provider="test", model="stub", dedup_enforce=True,
            )
            self.assertEqual(stored.status, "approved")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=self.category,
            ))
            # The caller can still quarantine the original DB record.
            self.assertEqual(db.get(Question, stored.id).status, "approved")

    def test_no_review_no_pool(self):
        self.seed_other_user_question(accepted=False)
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )):
            response = self.request()
        self.assertEqual(response.status_code, 503)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)

    def test_strict_too_easy_reserve_is_last_resort_and_not_in_future_pool(self):
        q = candidate()
        q._review_report = too_easy_report()

        def exhausted(*args, **kwargs):
            kwargs["too_easy_reserve"].append(q.model_copy(deep=True))
            raise HTTPException(status_code=502, detail="AI could not generate a valid question")

        with patch("api.generate_valid_question", side_effect=exhausted):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        with Session(self.engine) as db:
            review = db.scalar(select(QuestionReview))
            self.assertEqual(review.decision, "fallback_too_easy")
            self.assertEqual(review.details["fallback"]["original_decision"], "reject")
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            second = create_game(db, user_id=self.bob, language="English")
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=second.id, user_id=self.bob, category=self.category,
            ))

    def test_an_additional_penalty_excludes_reserve(self):
        q = candidate()
        q._review_report = too_easy_report()
        q._review_report["details"]["assessment"]["raw_penalties"]["distractors"] = 1

        def exhausted(*args, **kwargs):
            kwargs["too_easy_reserve"].append(q)
            raise HTTPException(status_code=502, detail="AI could not generate a valid question")

        with patch("api.generate_valid_question", side_effect=exhausted):
            response = self.request()
        self.assertEqual(response.status_code, 502)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 0)

    def test_pool_has_priority_over_too_easy_reserve(self):
        existing_id = self.seed_other_user_question()
        q = candidate(relation="prize", concept="prize", answer="photoelectric-effect")
        q._review_report = too_easy_report()

        def exhausted(*args, **kwargs):
            kwargs["too_easy_reserve"].append(q)
            raise HTTPException(status_code=503, detail="AI provider unavailable")

        with patch("api.generate_valid_question", side_effect=exhausted):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(existing_id))
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)


    def test_q14_switches_to_unused_category_after_generation_exhaustion(self):
        from persistence.models import GameRun, Topic
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            run.correct_main_answers = 13
            db.commit()
            primary = _category_for_round(self.run_id, 14, db=db)

        calls = []

        def generate(difficulty, language, category, **kwargs):
            calls.append((difficulty, category, kwargs["question_number"]))
            if len(calls) == 1:
                raise HTTPException(status_code=503, detail="AI provider unavailable")
            item = candidate(difficulty=difficulty)
            item._review_report = review_report()
            return item

        with patch("api.generate_valid_question", side_effect=generate):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["number"], 14)
        self.assertEqual(response.json()["difficulty"], 13)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], (13, primary, 14))
        self.assertNotEqual(calls[1][1], primary)
        self.assertEqual(calls[1][2], 14)
        with Session(self.engine) as db:
            question = db.get(Question, UUID(response.json()["question_id"]))
            chosen = db.get(Topic, question.topic_id).labels["en"]
            self.assertEqual(chosen, calls[1][1])
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)
        self.assertEqual(self.request().json(), response.json())

    def test_q14_uses_approved_alternate_pool_before_second_ai_call(self):
        from persistence.game_routes import _alternate_main_category
        from persistence.models import GameRun

        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            run.correct_main_answers = 13
            db.commit()
            primary = _category_for_round(self.run_id, 14, db=db)
            alternate = _alternate_main_category(db, run, 14, primary)
            self.assertIsNotNone(alternate)
            bob_run = create_game(db, user_id=self.bob, language="English")
            bob_run.correct_main_answers = 13
            db.commit()
            item = candidate(difficulty=13)
            item._review_report = review_report()
            _, stored = offer_question(
                db, run_id=bob_run.id, user_id=self.bob, question=item,
                category=alternate, provider="test", model="stub",
                dedup_enforce=True,
            )
            pooled_id = stored.id

        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )) as generator:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_id"], str(pooled_id))
        self.assertEqual(response.json()["number"], 14)
        generator.assert_called_once()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)

    def test_q14_double_generation_failure_preserves_active_round(self):
        from persistence.models import GameRun

        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            run.correct_main_answers = 13
            db.commit()

        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )) as generator:
            response = self.request()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(generator.call_count, 2)
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            self.assertEqual(run.status, "main")
            self.assertEqual(run.correct_main_answers, 13)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 0)

        item = candidate(difficulty=13)
        item._review_report = review_report()
        with patch("api.generate_valid_question", return_value=item):
            retry = self.request()
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["number"], 14)

    def test_q16_auto_rotates_before_second_category_generation(self):
        from persistence.models import GameRun

        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            run.status = "bonus"
            run.correct_main_answers = 15
            run.bonus_wager_accepted = True
            db.commit()
            primary = _category_for_round(self.run_id, 16, db=db)

        calls = []

        def generate(difficulty, language, category, **kwargs):
            calls.append((difficulty, category, kwargs["question_number"]))
            if len(calls) == 1:
                raise HTTPException(status_code=503, detail="AI provider unavailable")
            item = candidate(difficulty=15)
            item._review_report = review_report()
            return item

        with patch("api.generate_valid_question", side_effect=generate):
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["number"], 16)
        self.assertTrue(response.json()["bonus"])
        self.assertEqual(calls[0], (15, primary, 16))
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[1][1], primary)
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            self.assertEqual(run.bonus_topic_index, 1)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)

    def _seed_bonus_candidate(self, *, legacy=False, critic_too_easy=False,
                              arbitration_version="arbitration_v2"):
        """Create another user's actual Q16, preserving the same Question id."""
        from persistence.models import GameRun

        with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
            alice = db.get(GameRun, self.run_id)
            alice.status = "bonus"
            alice.correct_main_answers = 15
            alice.bonus_wager_accepted = True  # Isolate pool QA from wager UI.
            bob_run = create_game(db, user_id=self.bob, language="English")
            bob_run.status = "bonus"
            bob_run.correct_main_answers = 15
            bob_run.bonus_wager_accepted = True
            db.commit()
            q = candidate(difficulty=15)
            report = review_report()
            if not legacy:
                report["details"] = {
                    "verdict": {
                        "difficulty": "too_easy" if critic_too_easy else "appropriate",
                        "tags_valid": "pass", "fact_valid": "pass", "facts": "pass",
                        "options": "pass", "leakage": "pass",
                        "semantic_duplicate": False,
                    },
                    "assessment": {
                        "critical": "none", "total": 0.0, "score_threshold": 3.25,
                    },
                    "arbitration": {
                        "checker_type": "ai_terra_arbiter",
                        "provider": "openai", "model": "gpt-6.1-terra",
                        "contract_version": arbitration_version,
                        "decision": "accept",
                        "details": {
                            "premise": "valid",
                            "choices": {k: "yes" if k == "B" else "no"
                                        for k in "ABCD"},
                            "difficulty": "appropriate",
                            "answer_leak": False,
                            "stem_claims": [{
                                "claim": "Albert Einstein is the subject",
                                "anchor": "Albert Einstein",
                                "verdict": "supported",
                                "reason": "Named in the question",
                            }],
                            "rationale": "All conditions verified.",
                        },
                    },
                }
            q._review_report = report
            _, stored = offer_question(
                db, run_id=bob_run.id, user_id=self.bob, question=q,
                category=_category_for_round(self.run_id, 16),
                provider="test", model="stub",
                dedup_enforce=True,
            )
            return stored.id

    def test_old_bonus_approval_cannot_bypass_new_terra_gate(self):
        old_question_id = self._seed_bonus_candidate(legacy=True)
        with Session(self.engine) as db:
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=_category_for_round(self.run_id, 16),
                expected_bonus_topic_index=0,
            ))
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)
            self.assertEqual(db.get(Question, old_question_id).status, "approved")

    def test_new_terra_v2_bonus_can_be_reused_and_linked_to_user(self):
        existing_id = self._seed_bonus_candidate()
        with Session(self.engine) as db:
            attempt, saved = offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=_category_for_round(self.run_id, 16),
                expected_bonus_topic_index=0,
            )
            self.assertEqual(saved.id, existing_id)
            self.assertEqual(attempt.number, 16)
            self.assertEqual(attempt.question_id, existing_id)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)
            # A second user-level exposure is prevented by QuestionAttempt.
            self.assertEqual(db.get(Question, existing_id).status, "approved")

    def test_bonus_pool_blocks_too_easy_critic_even_with_terra_accept(self):
        self._seed_bonus_candidate(critic_too_easy=True)
        with Session(self.engine) as db:
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=_category_for_round(self.run_id, 16),
                expected_bonus_topic_index=0,
            ))

    def test_bonus_pool_rejects_obsolete_terra_v1(self):
        self._seed_bonus_candidate(arbitration_version="arbitration_v1")
        with Session(self.engine) as db:
            self.assertIsNone(offer_approved_pool_question(
                db, run_id=self.run_id, user_id=self.alice,
                category=_category_for_round(self.run_id, 16),
                expected_bonus_topic_index=0,
            ))

    def test_v2_verified_bonus_is_reused_by_live_fallback_route(self):
        existing_id = self._seed_bonus_candidate()
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )) as generator:
            reply = self.request()
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(reply.json()["bonus"])
        self.assertEqual(reply.json()["number"], 16)
        self.assertEqual(reply.json()["question_id"], str(existing_id))
        self.assertNotIn("correct_answer", reply.json())
        generator.assert_called_once()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 2)
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionReview.id))), 1)

    def test_old_bonus_fallback_returns_503_without_publishing(self):
        self._seed_bonus_candidate(legacy=True)
        with patch("api.generate_valid_question", side_effect=HTTPException(
            status_code=503, detail="AI provider unavailable",
        )) as generator:
            reply = self.request()
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertNotIn("question", reply.json())
        self.assertEqual(generator.call_count, 2)
        from persistence.models import GameRun
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            self.assertEqual(run.correct_main_answers, 15)
            self.assertEqual(run.bonus_topic_index, 1)
            self.assertIsNone(db.scalar(select(QuestionAttempt).where(
                QuestionAttempt.run_id == self.run_id
            )))

    def test_bonus_pool_rejects_stale_topic_index(self):
        from persistence.gameplay import GameConflict

        self._seed_bonus_candidate()
        with Session(self.engine) as db:
            with self.assertRaises(GameConflict):
                offer_approved_pool_question(
                    db, run_id=self.run_id, user_id=self.alice,
                    category=_category_for_round(self.run_id, 16),
                    expected_bonus_topic_index=1,
                )

if __name__ == "__main__":
    unittest.main()
