"""Adjacent category-family diversity with persisted v2 game state (offline)."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from category_taxonomy import CATEGORY_TOPICS, category_family, prefer_other_family
from consts import QUESTION_CATEGORIES
from persistence.categories import preceding_question_category
from persistence.database import get_db
from persistence.game_routes import _alternate_main_category, _category_for_round
from persistence.models import Base, GameRun, Question, QuestionAttempt, Topic
from persistence.seed_topics import category_slug, seed_topics
from question_schema import QuizQuestion


def fake_generate(difficulty, language, category, **kwargs):
    return QuizQuestion(
        difficulty=difficulty, question=f"Knowledge test: {category}?",
        answers={"A": "incorrect A", "B": "correct B",
                 "C": "incorrect C", "D": "incorrect D"},
        correct_answer="B",
    )


class AdjacentCategoryFamilyTests(unittest.TestCase):
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
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "false",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "false",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        reg = self.client.post("/api/auth/register", json={
            "username": "AdjacentQA", "password": "offline-test-password-2026!",
        })
        self.assertEqual(reg.status_code, 201, reg.text)
        game = self.client.post("/api/v2/games", json={"language": "English"})
        self.assertEqual(game.status_code, 201, game.text)
        self.run_id = UUID(game.json()["game_id"])
        self.base = f"/api/v2/games/{self.run_id}"

    def complete_slot(self, number, category, *, skipped=False,
                      revision=0, superseded=False):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as db:
            topic = db.scalar(select(Topic).where(Topic.slug == category_slug(category)))
            self.assertIsNotNone(topic)
            question = Question(
                topic_id=topic.id, language="English", requested_difficulty=number - 1,
                text=f"Offline regression {category}", choices={
                    "A": "A", "B": "B", "C": "C", "D": "D",
                }, correct_key="B", fingerprint=uuid4().hex,
                status="approved", generator_provider="test", generator_model="mock",
                prompt_version="test", schema_version="question_v1",
            )
            db.add(question)
            db.flush()
            db.add(QuestionAttempt(
                run_id=self.run_id, question_id=question.id,
                number=number, requested_difficulty=number - 1,
                revision=revision, answered_at=now,
                selected_key=None if skipped else "B",
                is_correct=None if skipped else True,
                superseded_at=now if superseded else None,
            ))
            db.commit()

    def test_shared_taxonomy_is_complete_and_blocks_related_subjects(self):
        self.assertEqual(set(CATEGORY_TOPICS), set(QUESTION_CATEGORIES))
        self.assertEqual(category_family("Literature"),
                         category_family("Authors and books"))
        self.assertEqual(category_family("Poetry"),
                         category_family("Authors and books"))
        self.assertEqual(category_family("Physics"),
                         category_family("Engineering"))
        self.assertEqual(category_family("Ancient history"),
                         category_family("Medieval history"))
        self.assertNotEqual(category_family("Literature"),
                            category_family("Visual arts"))

    def test_partition_keeps_relative_order_and_never_deadlocks(self):
        self.assertEqual(
            prefer_other_family(
                ["Authors and books", "Physics", "Poetry"], "Literature"
            ),
            ["Physics", "Authors and books", "Poetry"],
        )
        self.assertEqual(
            prefer_other_family(
                ["Authors and books", "Poetry"], "Literature"
            ),
            ["Authors and books", "Poetry"],
        )
        self.assertEqual(
            prefer_other_family(["Literature", "Physics"], None),
            ["Literature", "Physics"],
        )

    def test_previous_answer_changes_next_selection_and_survives_new_session(self):
        baseline = _category_for_round(self.run_id, 2)
        sibling = next(cat for cat in QUESTION_CATEGORIES
                       if cat != baseline and category_family(cat) == category_family(baseline))
        self.complete_slot(1, sibling)
        with Session(self.engine) as db:
            self.assertEqual(preceding_question_category(db, self.run_id, 2), sibling)
            current = _category_for_round(self.run_id, 2, db=db)
            self.assertNotEqual(category_family(current), category_family(sibling))
            self.assertNotEqual(current, sibling)
            self.assertEqual(_category_for_round(self.run_id, 2, db=db), current)
        with Session(self.engine) as restarted:
            self.assertEqual(_category_for_round(self.run_id, 2, db=restarted), current)

    def test_skipped_preceding_slot_still_blocks_family(self):
        baseline = _category_for_round(self.run_id, 2)
        sibling = next(cat for cat in QUESTION_CATEGORIES
                       if cat != baseline and category_family(cat) == category_family(baseline))
        self.complete_slot(1, sibling, skipped=True)
        with Session(self.engine) as db:
            self.assertEqual(preceding_question_category(db, self.run_id, 2), sibling)
            chosen = _category_for_round(self.run_id, 2, db=db)
            self.assertNotEqual(category_family(chosen), category_family(sibling))

    def test_superseded_version_is_not_the_previous_topic(self):
        self.complete_slot(1, "Literature", revision=0, superseded=True)
        self.complete_slot(1, "Modern history", revision=1)
        with Session(self.engine) as db:
            self.assertEqual(
                preceding_question_category(db, self.run_id, 2), "Modern history",
            )
            self.assertNotEqual(
                category_family(_category_for_round(self.run_id, 2, db=db)),
                category_family("Modern history"),
            )

    def test_late_generation_backup_respects_adjacent_family(self):
        self.complete_slot(12, "Literature")
        with Session(self.engine) as db:
            first = _category_for_round(self.run_id, 13, db=db)
            backup = _alternate_main_category(
                db, db.get(GameRun, self.run_id), 13, first,
            )
            self.assertIsNotNone(backup)
            self.assertNotEqual(first, backup)
            self.assertNotEqual(category_family(backup), category_family("Literature"))

    def test_bonus_rotation_still_visits_every_category(self):
        self.complete_slot(15, "Literature")
        with Session(self.engine) as db:
            run = db.get(GameRun, self.run_id)
            run.status = "bonus"
            run.correct_main_answers = 15
            db.commit()
        with Session(self.engine) as db:
            picks = [_category_for_round(self.run_id, 16, i, db=db)
                     for i in range(len(QUESTION_CATEGORIES))]
        self.assertNotEqual(category_family(picks[0]), category_family("Literature"))
        self.assertEqual(len(set(picks)), len(QUESTION_CATEGORIES))
        self.assertEqual(set(picks), set(QUESTION_CATEGORIES))

    def test_full_main_game_has_no_adjacent_related_categories(self):
        seen = []
        with patch("api.generate_valid_question", side_effect=fake_generate) as generator:
            for number in range(1, 16):
                response = self.client.get(self.base + "/question")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["number"], number)
                category = generator.call_args.args[2]
                if seen:
                    self.assertNotEqual(
                        category_family(seen[-1]), category_family(category),
                        f"Adjacent Q{number - 1}/Q{number}: {seen[-1]} / {category}",
                    )
                seen.append(category)
                answer = self.client.post(self.base + "/answer", json={
                    "question_id": response.json()["question_id"], "answer": "B",
                })
                self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(len(seen), 15)
        self.assertEqual(len(set(seen)), 15)


if __name__ == "__main__":
    unittest.main()
