"""Authenticated v2 PostgreSQL game API contract tests, offline on SQLite.

The real PostgreSQL migration/concurrency smoke test is separate.
No paid AI request is made.
"""
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from persistence.database import get_db
from persistence.models import (
    Base, GameRun, Question, QuestionAttempt, StarAward, WalletTransaction,
)
from persistence.seed_topics import seed_topics


QUESTION = {
    "difficulty": 0,
    "question": "Which planet is known as the Red Planet?",
    "answers": {"A": "Venus", "B": "Mars", "C": "Jupiter", "D": "Mercury"},
    "correct_answer": "B",
}


class PersistentGameAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def fk_on(connection, _):
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
            "AI_TAG_DEDUP_ENABLED": "false",  # Legacy v2 test fixture explicitly uses the old contract.
            "AI_QUALITY_MODE": "off",
            "AI_LOCAL_LEAK_MODE": "off",
            "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12",
            "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        }))
        self.enterContext(patch("api.sleep"))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def register(self, name, client=None):
        result = (client or self.client).post("/api/auth/register", json={
            "username": name, "password": "offline-test-password-2026!",
        })
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["id"]

    def start(self, language="English", client=None):
        response = (client or self.client).post(
            "/api/v2/games", json={"language": language}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["game_id"]

    def test_active_round_is_discovered_without_browser_storage_or_cookie_locality(self):
        self.assertEqual(self.client.get("/api/v2/games/active").status_code, 401)
        user_id = self.register("Alice")
        empty = self.client.get("/api/v2/games/active")
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertIsNone(empty.json())

        run_id = self.start("Russian")
        # Cookies can be recreated by logging in through a different browser
        # origin; the server must find the same user-owned run without any ID.
        second = TestClient(app)
        self.addCleanup(second.close)
        login = second.post("/api/auth/login", json={
            "username": "Alice", "password": "offline-test-password-2026!",
        })
        self.assertEqual(login.status_code, 200, login.text)
        found = second.get("/api/v2/games/active")
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["game_id"], run_id)
        self.assertFalse(found.json()["question_issued"])
        self.assertEqual(found.json()["language"], "Russian")

        # A different user must never discover Alice's round.
        third = TestClient(app)
        self.addCleanup(third.close)
        self.register("Bob", client=third)
        self.assertIsNone(third.get("/api/v2/games/active").json())

    def test_active_round_restores_purchased_helper_and_category_exclusions(self):
        user_id = UUID(self.register("Alice"))
        run_id = self.start("English")
        with Session(self.engine) as db:
            db.add(StarAward(user_id=user_id, run_id=UUID(run_id), tier="platinum"))
            db.add(WalletTransaction(
                user_id=user_id, run_id=UUID(run_id),
                reason="star/silver/test-grant", amount=5,
                idempotency_key="setup-recovery-five-silver",
            ))
            db.commit()
        purchase = self.client.post(
            f"/api/v2/games/{run_id}/helper/purchase",
            json={"profession": None, "ui_language": "ru"},
        )
        self.assertEqual(purchase.status_code, 201, purchase.text)
        exclusions = self.client.post(f"/api/v2/games/{run_id}/categories/exclude", json={
            "categories": ["Physics"], "request_id": str(uuid4()),
        })
        self.assertEqual(exclusions.status_code, 200, exclusions.text)
        found = self.client.get("/api/v2/games/active")
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["game_id"], run_id)
        self.assertEqual(found.json()["helper"]["name"], purchase.json()["name"])
        self.assertFalse(found.json()["question_issued"])
        categories = self.client.get(f"/api/v2/games/{run_id}/categories").json()
        self.assertNotIn("Physics", categories["allowed"])
        self.assertEqual(self.client.get("/api/v2/stars").json()["platinum"], 0)

        # The ordinary creation path cannot abandon/multiply this paid round.
        blocked = self.client.post("/api/v2/games", json={"language": "Spanish"})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(self.client.get("/api/v2/games/active").json()["game_id"], run_id)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(GameRun.id))), 1)

    def test_active_lookup_uses_newest_legacy_run_and_replacement(self):
        user_id = UUID(self.register("Alice"))
        # Legacy versions allowed multiple open runs; all must remain intact.
        first = self.start("English")
        with Session(self.engine) as db:
            older = db.get(GameRun, UUID(first))
            older.started_at = older.started_at.replace(year=2024)
            newer = GameRun(user_id=user_id, language="Hebrew", status="main")
            db.add(newer)
            db.commit()
            newer_id = str(newer.id)
        active = self.client.get("/api/v2/games/active")
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(active.json()["game_id"], newer_id)

        updated = self.client.post(
            f"/api/v2/games/{newer_id}/replace", json={"language": "French"},
        )
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertEqual(self.client.get("/api/v2/games/active").json()["game_id"],
                         updated.json()["game_id"])

        with Session(self.engine) as db:
            db.get(GameRun, UUID(first)).status = "completed"
            db.get(GameRun, UUID(updated.json()["game_id"])).status = "completed"
            db.commit()
        self.assertIsNone(self.client.get("/api/v2/games/active").json())

    def test_abandon_with_helper_and_exclusions_preserves_wallet_and_creates_setup(self):
        user_id = UUID(self.register("Alice"))
        run_id = self.start("English")
        with Session(self.engine) as db:
            db.add(StarAward(user_id=user_id, run_id=UUID(run_id), tier="platinum"))
            db.add(StarAward(user_id=user_id, run_id=UUID(run_id), tier="bronze"))
            db.add(WalletTransaction(
                user_id=user_id, run_id=UUID(run_id),
                reason="star/silver/test-grant", amount=5,
                idempotency_key="setup-test-five-silver",
            ))
            db.commit()
        helper = self.client.post(
            f"/api/v2/games/{run_id}/helper/purchase",
            json={"profession": None, "ui_language": "ru"},
        )
        self.assertEqual(helper.status_code, 201, helper.text)
        self.assertRegex(helper.json()["name"], r"[А-Яа-яЁё]")
        self.assertEqual(self.client.get("/api/v2/stars").json()["platinum"], 0)
        exclude = self.client.post(f"/api/v2/games/{run_id}/categories/exclude", json={
            "categories": ["Physics"], "request_id": str(uuid4()),
        })
        self.assertEqual(exclude.status_code, 200, exclude.text)
        self.assertEqual(self.client.get("/api/v2/stars").json()["silver"], 0)
        before = self.client.get(f"/api/v2/games/{run_id}").json()
        self.assertFalse(before["question_issued"])
        self.assertIsNotNone(before["helper"])
        self.assertEqual(before["helper"]["id"], helper.json()["id"])

        replaced = self.client.post(f"/api/v2/games/{run_id}/replace", json={
            "language": "German",
        })
        self.assertEqual(replaced.status_code, 201, replaced.text)
        fresh = replaced.json()
        self.assertNotEqual(fresh["game_id"], run_id)
        self.assertEqual(fresh["language"], "German")
        self.assertFalse(fresh["question_issued"])
        self.assertIsNone(fresh["helper"])
        self.assertEqual(fresh["main_question_index"], 0)
        self.assertEqual(fresh["phase"], "main")
        self.assertEqual(
            self.client.get(f"/api/v2/games/{fresh['game_id']}/categories").json()["can_exclude"],
            True,
        )
        old = self.client.get(f"/api/v2/games/{run_id}").json()
        self.assertEqual(old["phase"], "abandoned")
        self.assertEqual(old["replacement_game_id"], fresh["game_id"])
        self.assertIsNotNone(old["helper"])  # History remains inspectable.
        self.assertEqual(
            self.client.get(f"/api/v2/games/{run_id}/question").status_code, 409,
        )
        self.assertEqual(
            self.client.get("/api/v2/stars").json(),
            {"bronze": 1, "silver": 0, "gold": 0, "platinum": 0},
        )  # Earned bronze is still there; spent silver/platinum do not refund.

        # An uncertain duplicate POST must not generate an extra game, re-charge
        # perks, or apply a later request's different language to the successor.
        retry = self.client.post(f"/api/v2/games/{run_id}/replace", json={
            "language": "Italian",
        })
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["game_id"], fresh["game_id"])
        self.assertEqual(retry.json()["language"], "German")
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(GameRun.id))), 2)
            self.assertIsNotNone(db.get(GameRun, UUID(run_id)).finished_at)

    def test_category_exclusions_first_then_helper_and_question_language_changes(self):
        user_id = UUID(self.register("Alice"))
        run_id = self.start("Spanish")
        with Session(self.engine) as db:
            db.add(StarAward(user_id=user_id, run_id=UUID(run_id), tier="platinum"))
            db.add(WalletTransaction(
                user_id=user_id, run_id=UUID(run_id),
                reason="star/silver/test-grant", amount=5,
                idempotency_key="setup-test-silver-before-helper",
            ))
            db.commit()
        exclude = self.client.post(f"/api/v2/games/{run_id}/categories/exclude", json={
            "categories": ["Physics"], "request_id": str(uuid4()),
        })
        self.assertEqual(exclude.status_code, 200, exclude.text)
        language = self.client.patch(f"/api/v2/games/{run_id}/language", json={
            "language": "German",
        })
        self.assertEqual(language.status_code, 200, language.text)
        helper = self.client.post(f"/api/v2/games/{run_id}/helper/purchase", json={
            "profession": None, "ui_language": "ru",
        })
        self.assertEqual(helper.status_code, 201, helper.text)
        self.assertRegex(helper.json()["name"], r"[А-Яа-яЁё]")
        saved = self.client.get(f"/api/v2/games/{run_id}").json()
        self.assertEqual(saved["helper"], helper.json())
        self.assertEqual(saved["language"], "German")
        self.assertFalse(saved["question_issued"])
        self.assertNotIn(
            "Physics",
            self.client.get(f"/api/v2/games/{run_id}/categories").json()["allowed"],
        )
        self.assertEqual(self.client.get("/api/v2/stars").json()["platinum"], 0)

        # The helper's stored name never changes when the question language does.
        changed = self.client.patch(f"/api/v2/games/{run_id}/language", json={
            "language": "Italian",
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["helper"], helper.json())
        self.assertEqual(changed.json()["language"], "Italian")
        invalid_locale = self.client.post(f"/api/v2/games/{run_id}/helper/purchase", json={
            "profession": None, "ui_language": "unsupported",
        })
        self.assertEqual(invalid_locale.status_code, 422)
        self.assertEqual(self.client.get("/api/v2/stars").json()["platinum"], 0)

    def test_abandon_invalid_language_and_cross_user_are_non_mutating(self):
        self.register("Alice")
        run_id = self.start("English")
        invalid = self.client.post(f"/api/v2/games/{run_id}/replace", json={
            "language": "Not-a-game-language",
        })
        self.assertEqual(invalid.status_code, 422)
        csrf = self.client.post(
            f"/api/v2/games/{run_id}/replace",
            json={"language": "English"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(csrf.status_code, 403)
        other = TestClient(app)
        self.addCleanup(other.close)
        self.register("Bob", client=other)
        forbidden = other.post(f"/api/v2/games/{run_id}/replace", json={
            "language": "English",
        })
        self.assertEqual(forbidden.status_code, 404)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(GameRun.id))), 1)
            self.assertEqual(db.get(GameRun, UUID(run_id)).status, "main")

    def test_game_endpoints_require_login(self):
        self.assertEqual(self.client.post("/api/v2/games", json={}).status_code, 401)
        self.assertEqual(self.client.get(
            f"/api/v2/games/{uuid4()}"
        ).status_code, 401)
        self.assertEqual(self.client.get(
            f"/api/v2/games/{uuid4()}/question"
        ).status_code, 401)
        self.assertEqual(self.client.post(
            f"/api/v2/games/{uuid4()}/answer", json={
                "question_id": str(uuid4()), "answer": "A",
            }
        ).status_code, 401)

    def test_cross_site_cannot_start_or_answer(self):
        self.register("Alice")
        response = self.client.post("/api/v2/games", json={},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        game_id = self.start()
        response = self.client.post(
            f"/api/v2/games/{game_id}/answer",
            json={"question_id": str(uuid4()), "answer": "A"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_language_rejected_before_game_created(self):
        self.register("Alice")
        self.assertEqual(self.client.post(
            "/api/v2/games", json={"language": "Klingon"}
        ).status_code, 422)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(GameRun.id))), 0)

    @patch("api.create_question", return_value=QUESTION)
    def test_question_is_private_cached_and_survives_new_client(self, gen):
        self.register("Alice")
        game_id = self.start()
        url = f"/api/v2/games/{game_id}/question"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["difficulty"], 0)
        self.assertNotIn("correct_answer", first.json())
        self.assertEqual(self.client.get(url).json(), first.json())
        with TestClient(app) as restarted:
            restarted.cookies.update(self.client.cookies)
            self.assertEqual(restarted.get(url).json(), first.json())
        gen.assert_called_once()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)
            self.assertEqual(db.scalar(select(func.count(QuestionAttempt.id))), 1)

    @patch("api.create_question", return_value=QUESTION)
    def test_answer_persists_stats_and_rejects_replay(self, gen):
        self.register("Alice")
        game_id = self.start()
        url = f"/api/v2/games/{game_id}"
        question = self.client.get(url + "/question").json()
        response = self.client.post(url + "/answer", json={
            "question_id": question["question_id"], "answer": "B",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["correct"])
        self.assertEqual(response.json()["correct_answer"], "B")
        self.assertEqual(response.json()["correct_main_answers"], 1)
        self.assertEqual(self.client.post(url + "/answer", json={
            "question_id": question["question_id"], "answer": "B",
        }).status_code, 409)
        self.assertEqual(self.client.get(url).json()["correct_main_answers"], 1)
        stats = self.client.get("/api/profile/me/stats")
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertEqual(stats.json()["answered_questions"], 1)
        self.assertEqual(stats.json()["correct_answers"], 1)
        self.assertEqual(stats.json()["games_played"], 1)
        self.assertEqual(stats.json()["wallet_balance"], 0)
        gen.assert_called_once()

    @patch("api.create_question", return_value=QUESTION)
    def test_other_user_cannot_view_answer_or_mutate_game(self, gen):
        self.register("Alice")
        game_id = self.start()
        question_id = self.client.get(
            f"/api/v2/games/{game_id}/question"
        ).json()["question_id"]
        with TestClient(app) as bob:
            self.register("Bob", client=bob)
            self.assertEqual(bob.get(f"/api/v2/games/{game_id}").status_code, 404)
            self.assertEqual(bob.get(
                f"/api/v2/games/{game_id}/question"
            ).status_code, 404)
            self.assertEqual(bob.post(
                f"/api/v2/games/{game_id}/answer",
                json={"question_id": question_id, "answer": "B"}
            ).status_code, 404)
        self.assertEqual(self.client.post(
            f"/api/v2/games/{game_id}/answer",
            json={"question_id": question_id, "answer": "B"}
        ).status_code, 200)

    @patch("api.create_question", return_value=QUESTION)
    def test_wrong_id_does_not_consume_question(self, gen):
        self.register("Alice")
        game_id = self.start()
        url = f"/api/v2/games/{game_id}"
        original = self.client.get(url + "/question").json()
        bad = self.client.post(url + "/answer", json={
            "question_id": str(uuid4()), "answer": "B",
        })
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(self.client.get(url + "/question").json(), original)

    @patch("api.create_question")
    def test_quality_validation_retries_before_persistence(self, gen):
        self.register("Alice")
        game_id = self.start()
        gen.side_effect = [
            {**QUESTION, "difficulty": 5},
            QUESTION,
        ]
        response = self.client.get(f"/api/v2/games/{game_id}/question")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(gen.call_count, 2)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Question.id))), 1)

    @patch("api.create_question", return_value=QUESTION)
    def test_achievements_are_transactional_and_stats_show_them(self, gen):
        self.register("Alice")
        game_id = self.start()
        gen.side_effect = lambda level, language, category: {
            **QUESTION, "difficulty": level,
        }
        url = f"/api/v2/games/{game_id}"
        for _ in range(5):
            current = self.client.get(url + "/question")
            self.assertEqual(current.status_code, 200, current.text)
            scored = self.client.post(url + "/answer", json={
                "question_id": current.json()["question_id"], "answer": "B",
            })
            self.assertEqual(scored.status_code, 200, scored.text)
        self.assertEqual(scored.json()["achievement"], "bronze")
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(StarAward.id))), 1)
        self.assertEqual(
            self.client.get("/api/profile/me/stats").json()["star_awards"],
            {"bronze": 1},
        )


if __name__ == "__main__":
    unittest.main()
