"""Offline regression of question competence resolution and one-time backfill.

No live provider calls, no production DB and no Star/perk mutations.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from persistence.models import Base, Question, QuestionFact, Topic
from platinum_helper.competence import competence_for_question, infer_legacy_area
from platinum_helper.core import TOPICS
from scripts import backfill_question_competence as backfill


class FakeOpenAI:
    def __init__(self):
        self.calls = []
        self.responses = self

    def with_options(self, **kwargs):
        assert kwargs == {"timeout": 20, "max_retries": 0}
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        items = json.loads(kwargs["input"])["questions"]
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps({"results": [
                {"id": item["id"], "competence_area": "Medicine"} for item in items
            ]}),
        )


class CompetenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False, autoflush=False)
        topic = Topic(slug="inventions-and-discoveries",
                      labels={"en": "Inventions and discoveries"})
        self.db.add(topic)
        self.db.flush()
        self.faraday = Question(
            topic_id=topic.id, language="German", requested_difficulty=3,
            text="Wer entdeckte die elektromagnetische Induktion?",
            choices={"A": "Michael Faraday", "B": "André-Marie Ampère",
                     "C": "Nikola Tesla", "D": "Isaac Newton"},
            correct_key="A", fingerprint="a" * 64, status="approved",
            generator_provider="test", generator_model="test",
            prompt_version="test", schema_version="test",
        )
        self.untagged = Question(
            topic_id=topic.id, language="German", requested_difficulty=4,
            text="Welches Mittel wurde in der Medizin entdeckt?",
            choices={"A": "Penicillin", "B": "Ammoniak",
                     "C": "Gummi", "D": "Aluminium"},
            correct_key="A", fingerprint="b" * 64, status="approved",
            generator_provider="test", generator_model="test",
            prompt_version="test", schema_version="test",
        )
        self.db.add_all([self.faraday, self.untagged])
        self.db.flush()
        self.db.add(QuestionFact(
            question_id=self.faraday.id, fact_key="c" * 64,
            subject="electromagnetic-induction", relation="discoverer",
            object="michael-faraday",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_resolve_from_canonical_metadata_before_generic_game_category(self):
        area, source = competence_for_question(
            self.db, self.faraday, "Inventions and discoveries"
        )
        self.assertEqual((area, source), ("Physics", "legacy_metadata"))
        self.assertEqual(
            competence_for_question(self.db, self.untagged, "Inventions and discoveries"),
            ("Technology", "legacy_category"),
        )
        self.assertEqual(infer_legacy_area("penicillin-discovery"), "Medicine")
        self.assertEqual(infer_legacy_area("printing-press"), "Technology")
        self.assertEqual(infer_legacy_area("alien-entity"), None)
        self.assertIsNone(infer_legacy_area(
            "electromagnetic-induction", ["penicillin-discovery"]
        ))
        self.assertEqual(
            infer_legacy_area("michael-faraday", object_slug="electromagnetic-induction"),
            "Physics",
        )

    def test_schema_uses_shared_twenty_skill_enum(self):
        self.assertEqual(
            set(backfill._schema()["properties"]["results"]["items"]
                ["properties"]["competence_area"]["enum"]),
            set(TOPICS),
        )

    def test_free_preview_and_no_AI_without_explicit_flag(self):
        fake = FakeOpenAI()
        self.assertEqual(backfill.run(["--limit", "3"], db=self.db, client=fake), 0)
        self.assertIsNone(self.faraday.competence_area)
        self.assertIsNone(self.untagged.competence_area)
        self.assertFalse(fake.calls)

    def test_apply_only_high_confidence_tags_and_leave_untagged_for_review(self):
        fake = FakeOpenAI()
        self.assertEqual(backfill.run(["--apply", "--limit", "3"],
                                      db=self.db, client=fake), 0)
        self.assertEqual(self.faraday.competence_area, "Physics")
        self.assertIsNone(self.untagged.competence_area)
        self.assertFalse(fake.calls)

    def test_paid_AI_only_with_apply_and_preserves_prior_assignment(self):
        fake = FakeOpenAI()
        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "offline-test-no-real-key",
            "AI_QUALITY_MODEL_ID": "gpt-5.4-mini",
        }):
            with self.assertRaises(SystemExit):
                backfill.run(["--ai"], db=self.db, client=fake)
            self.assertEqual(backfill.run(
                ["--apply", "--ai", "--limit", "3"], db=self.db, client=fake,
            ), 0)
        self.assertEqual(self.faraday.competence_area, "Physics")
        self.assertEqual(self.untagged.competence_area, "Medicine")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], "gpt-5.4-mini")
        self.assertEqual(
            fake.calls[0]["text"]["format"]["schema"]["additionalProperties"],
            False,
        )

    def test_invalid_paid_output_never_creates_database_assignment(self):
        class Incomplete(FakeOpenAI):
            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    status="completed", output_text='{"results":[]}'
                )
        fake = Incomplete()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test"}):
            with self.assertRaises(ValueError):
                backfill.run(
                    ["--apply", "--ai", "--limit", "3"], db=self.db, client=fake,
                )
        self.assertEqual(self.faraday.competence_area, "Physics")
        self.assertIsNone(self.untagged.competence_area)


if __name__ == "__main__":
    unittest.main()
