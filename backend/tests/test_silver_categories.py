"""Silver star integration checks with mocked generator and in-memory SQLite."""
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from consts import QUESTION_CATEGORIES
from persistence.database import get_db
from persistence.models import Base, GameRun, QuestionAttempt, User, WalletTransaction
from persistence.seed_topics import seed_topics
from question_schema import QuizQuestion


def fake_generate(difficulty, language, category, **kwargs):
    return QuizQuestion(
        difficulty=difficulty, question=f"Which is the answer for {category}?",
        answers={"A": "wrong one", "B": "correct", "C": "wrong two", "D": "wrong three"},
        correct_answer="B",
    )


class SilverCategoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def fk_on(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
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
            "AI_TAG_DEDUP_ENABLED": "false",
            "AI_QUALITY_MODE": "off",
            "AI_LOCAL_LEAK_MODE": "off",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        res = self.client.post("/api/auth/register", json={
            "username": "SilverTester", "password": "offline-test-password-2026!",
        })
        self.assertEqual(res.status_code, 201, res.text)
        self.user_id = UUID(res.json()["id"])
        res = self.client.post("/api/v2/games", json={"language": "English"})
        self.assertEqual(res.status_code, 201, res.text)
        self.run_id = res.json()["game_id"]
        self.base = f"/api/v2/games/{self.run_id}"
        self.generator = self.enterContext(patch(
            "api.generate_valid_question", side_effect=fake_generate,
        ))

    def credit(self, count):
        with Session(self.engine) as db:
            db.add(WalletTransaction(
                user_id=self.user_id, reason="star/silver/test-credit",
                amount=count, idempotency_key=str(uuid4())))
            db.commit()

    def silver(self):
        return self.client.get("/api/v2/stars").json()["silver"]

    def first_question(self):
        res = self.client.get(self.base + "/question")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def change(self, q, mode, category=None, request_id=None):
        return self.client.post(self.base + "/categories/change", json={
            "question_id": q["question_id"], "mode": mode,
            "category": category, "request_id": str(request_id or uuid4()),
        })

    def test_exclude_before_question_with_cost_and_all_future_categories_allowed(self):
        self.credit(10)
        info = self.client.get(self.base + "/categories").json()
        self.assertTrue(info["can_exclude"])
        self.assertEqual(len(info["allowed"]), len(QUESTION_CATEGORIES))
        excluded = [QUESTION_CATEGORIES[0], QUESTION_CATEGORIES[1]]
        request_id = str(uuid4())
        resp = self.client.post(self.base + "/categories/exclude", json={
            "categories": excluded, "request_id": request_id,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["allowed"]), len(QUESTION_CATEGORIES) - 2)
        self.assertTrue(set(excluded).isdisjoint(resp.json()["allowed"]))
        self.assertEqual(self.silver(), 0)
        replay = self.client.post(self.base + "/categories/exclude", json={
            "categories": excluded, "request_id": request_id,
        })
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(self.silver(), 0)
        self.first_question()
        self.assertNotIn(self.generator.call_args.args[2], excluded)
        failure = self.client.post(self.base + "/categories/exclude", json={
            "categories": [QUESTION_CATEGORIES[2]], "request_id": str(uuid4()),
        })
        self.assertEqual(failure.status_code, 409)

    def test_paid_exclusions_survive_language_change_before_q1(self):
        self.credit(5)
        removed = QUESTION_CATEGORIES[0]
        excluded = self.client.post(self.base + "/categories/exclude", json={
            "categories": [removed], "request_id": str(uuid4()),
        })
        self.assertEqual(excluded.status_code, 200, excluded.text)
        self.assertEqual(self.silver(), 0)
        allowed = excluded.json()["allowed"]
        self.assertNotIn(removed, allowed)

        changed = self.client.patch(self.base + "/language", json={
            "language": "Russian",
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["game_id"], self.run_id)
        self.assertEqual(changed.json()["language"], "Russian")
        self.assertEqual(self.client.get(self.base + "/categories").json()["allowed"], allowed)
        self.assertEqual(self.silver(), 0)
        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.run_id))
            self.assertEqual(run.language, "Russian")
            self.assertEqual(run.allowed_categories, allowed)
        self.first_question()
        self.assertEqual(self.generator.call_args.args[1], "Russian")
        self.assertNotIn(self.generator.call_args.args[2], [removed])
        # The first question is already issued; changing it would mix languages.
        self.assertEqual(self.client.patch(
            self.base + "/language", json={"language": "English"},
        ).status_code, 409)

    def test_language_change_rejects_invalid_language_and_foreign_run(self):
        self.assertEqual(self.client.patch(
            self.base + "/language", json={"language": "Klingon"},
        ).status_code, 422)
        self.assertEqual(self.client.get(self.base).json()["language"], "English")
        with TestClient(app) as stranger:
            registered = stranger.post("/api/auth/register", json={
                "username": "ForeignLanguageUser",
                "password": "offline-test-password-2026!",
            })
            self.assertEqual(registered.status_code, 201, registered.text)
            self.assertEqual(stranger.patch(
                self.base + "/language", json={"language": "Russian"},
            ).status_code, 404)
        self.assertEqual(self.client.get(self.base).json()["language"], "English")

    def test_language_change_when_sixteen_categories_remain(self):
        removed = QUESTION_CATEGORIES[:-16]
        self.credit(len(removed) * 5)
        result = self.client.post(self.base + "/categories/exclude", json={
            "categories": removed, "request_id": str(uuid4()),
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["can_exclude"])
        allowed = result.json()["allowed"]
        changed = self.client.patch(self.base + "/language", json={
            "language": "Russian",
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get(self.base + "/categories").json()["allowed"], allowed)
        self.assertEqual(self.silver(), 0)
        self.first_question()
        self.assertEqual(self.generator.call_args.args[1], "Russian")

    def test_exactly_sixteen_categories_disable_swaps(self):
        remove = QUESTION_CATEGORIES[:-16]
        self.credit(len(remove) * 5)
        resp = self.client.post(self.base + "/categories/exclude", json={
            "categories": remove, "request_id": str(uuid4()),
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["allowed"]), 16)
        self.assertFalse(resp.json()["can_exclude"])
        question = self.first_question()
        result = self.change(question, "random")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.silver(), 0)

    def test_random_and_manual_switches_share_two_limit(self):
        self.credit(4)
        first = self.first_question()
        before = self.client.get(self.base + "/categories").json()
        self.assertTrue(before["can_choose"])
        manual_topic = before["alternatives"][0]
        chosen = self.change(first, "manual", manual_topic)
        self.assertEqual(chosen.status_code, 200, chosen.text)
        q2 = chosen.json()
        self.assertNotEqual(q2["question_id"], first["question_id"])
        self.assertEqual(q2["number"], 1)
        state = self.client.get(self.base + "/categories").json()
        self.assertEqual(state["changes_used"], 1)
        self.assertEqual(state["manual_used"], 1)
        self.assertFalse(state["can_choose"])
        self.assertTrue(state["can_change"])
        self.assertEqual(state["current"], manual_topic)
        self.assertEqual(self.silver(), 2)

        changed = self.change(q2, "random")
        self.assertEqual(changed.status_code, 200, changed.text)
        q3 = changed.json()
        self.assertEqual(q3["number"], 1)
        self.assertEqual(q3["difficulty"], 0)
        state = self.client.get(self.base + "/categories").json()
        self.assertEqual(state["changes_used"], 2)
        self.assertFalse(state["can_change"])
        self.assertFalse(state["can_choose"])
        self.assertEqual(self.silver(), 1)
        self.assertEqual(self.change(q3, "random").status_code, 409)

        self.assertEqual(self.client.post(self.base + "/answer", json={
            "question_id": first["question_id"], "answer": "B",
        }).status_code, 409)
        self.assertEqual(self.client.post(self.base + "/answer", json={
            "question_id": q3["question_id"], "answer": "B",
        }).status_code, 200)

        with Session(self.engine) as db:
            run = db.get(GameRun, UUID(self.run_id))
            self.assertEqual(run.hints_used, 2)
            attempts = db.scalars(select(QuestionAttempt)
                .where(QuestionAttempt.run_id == run.id)
                .order_by(QuestionAttempt.revision)).all()
            self.assertEqual(len(attempts), 3)
            self.assertEqual([a.revision for a in attempts], [0, 1, 2])
            self.assertTrue(attempts[0].superseded_at)
            self.assertTrue(attempts[1].superseded_at)
            self.assertIsNone(attempts[2].superseded_at)

        second = self.first_question()
        self.assertEqual(second["number"], 2)
        fresh = self.client.get(self.base + "/categories").json()
        self.assertEqual((fresh["changes_used"], fresh["manual_used"]), (0, 0))

    def test_paid_random_change_leaves_current_subject_family(self):
        from category_taxonomy import category_family

        self.credit(2)
        with patch("persistence.game_routes._category_for_round", return_value="Biology"):
            first = self.first_question()
        self.assertEqual(self.generator.call_args.args[2], "Biology")

        # Capture the actual random pool presented by the live route.
        with patch("persistence.game_routes.secrets.choice",
                   side_effect=lambda choices: choices[0]) as choose:
            replaced = self.change(first, "random")
        self.assertEqual(replaced.status_code, 200, replaced.text)
        candidates = choose.call_args.args[0]
        self.assertTrue(candidates)
        self.assertTrue(all(
            category_family(cat) != category_family("Biology")
            for cat in candidates
        ), candidates)
        self.assertNotEqual(
            category_family(self.generator.call_args.args[2]),
            category_family("Biology"),
        )
        self.assertEqual(self.silver(), 1)
        self.assertEqual(
            self.client.get(self.base + "/categories").json()["changes_used"], 1,
        )
        self.assertNotEqual(replaced.json()["question_id"], first["question_id"])

    def test_failed_generation_does_not_charge_or_replace_question(self):
        self.credit(2)
        q = self.first_question()
        with patch("api.generate_valid_question", side_effect=HTTPException(
                status_code=503, detail="Unavailable")):
            resp = self.change(q, "random")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(self.silver(), 2)
        self.assertEqual(self.client.get(self.base + "/question").json(), q)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)
        self.assertEqual(self.client.get(self.base + "/categories").json()["changes_used"], 0)

    def test_wrong_user_cannot_see_or_change_categories(self):
        with TestClient(app) as other:
            r = other.post("/api/auth/register", json={
                "username": "SecondPlayer", "password": "offline-test-password-2026!",
            })
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(other.get(self.base + "/categories").status_code, 404)
            self.assertEqual(other.post(self.base + "/categories/exclude", json={
                "categories": [QUESTION_CATEGORIES[0]], "request_id": str(uuid4()),
            }).status_code, 404)


if __name__ == "__main__":
    unittest.main()
