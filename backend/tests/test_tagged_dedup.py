"""Strict offline tests for language-independent fact dedup and tagged critic.

No database server or paid AI requests required.
"""
import json
import os
import unittest
from time import perf_counter
from unittest.mock import Mock, patch

from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from generation_policy import InvalidQuestionError
from output_contracts import OutputContract, transport_contract
from persistence.gameplay import create_game, offer_question
from persistence.models import (
    Base, Question, QuestionAttempt, QuestionFact, QuestionReview, QuestionTag, Tag, Topic, User,
)
from persistence.seed_topics import category_slug
from quality_errors import QualityRegenerationError, QualityReviewFailure
from quality_schema import QualityMode
from question_schema import QuizQuestion
from tagging.contract import TaggedQuestion
from tagging.core import (
    DuplicateQuestionError, HistoryItem, HistoryNotTagged, rank, require_unique,
    similarity, threshold,
)
from tagging.repository import history_for_user
from tagging.review import (
    TaggedReview, check_tagged_quality, option_evidence_diagnostics, tagged_assessment,
)
from tagging.schemas import (
    tagged_question_schema, tagged_review_schema, tag_extraction_schema,
)


def candidate(*, language="English", relation="birth-year",
              concept="birth-date", answer="1879", difficulty=0):
    if relation == "birth-year":
        question = ("In what year was Albert Einstein born?" if language == "English"
                    else "In welchem Jahr wurde Albert Einstein geboren?")
        answers = {"A": "1878", "B": "1879", "C": "1880", "D": "1881"}
    else:
        question = "For what scientific work did Albert Einstein win the Nobel Prize?"
        answers = {"A": "Relativity", "B": "Photoelectric effect",
                   "C": "Nuclear fission", "D": "Brownian motion"}
    return TaggedQuestion.model_validate({
        "difficulty": difficulty,
        "question": question,
        "answers": answers,
        "correct_answer": "B",
        "tags": {
            "entity": ["albert-einstein"], "concept": [concept],
            "keyword": ["physicist"],
        },
        "fact": {
            "subject": "albert-einstein", "relation": relation, "object": answer,
        },
    })


class TaggedContractTests(unittest.TestCase):
    def test_v2_transport_is_separate_from_legacy_question_contract(self):
        from question_transport_schema import quiz_question_schema
        self.assertNotIn("fact", quiz_question_schema()["properties"])
        self.assertEqual(transport_contract(OutputContract.TAGGED_QUESTION)[0], "quiz_question_tagged_v3")
        self.assertEqual(set(tagged_question_schema()["properties"]["plausibility"]["required"]), set("ABCD"))
        schema = tagged_question_schema()
        self.assertEqual(set(schema["properties"]), set(TaggedQuestion.model_fields))
        self.assertEqual(set(schema["required"]), set(TaggedQuestion.model_fields))
        self.assertEqual(set(tag_extraction_schema()["properties"]), {"tags", "fact"})
        self.assertIn("semantic_duplicate", tagged_review_schema()["required"])
        review_name, review_schema = transport_contract(OutputContract.TAGGED_REVIEW)
        self.assertEqual(review_name, "quiz_tagged_review_v5")
        self.assertEqual(set(review_schema["required"]), set(TaggedReview.model_fields))
        self.assertEqual(set(review_schema["properties"]), set(TaggedReview.model_fields))
        self.assertFalse(review_schema["additionalProperties"])
        self.assertEqual(review_schema["properties"]["premise"]["enum"],
                         ["valid", "invalid", "uncertain"])
        option_schema = review_schema["properties"]["option_assessments"]
        self.assertFalse(option_schema["additionalProperties"])
        self.assertEqual(option_schema["required"], list("ABCD"))
        for key in "ABCD":
            item = option_schema["properties"][key]
            self.assertFalse(item["additionalProperties"])
            self.assertEqual(item["required"], ["status", "rationale"])
            self.assertEqual(item["properties"]["status"]["enum"],
                             ["yes", "no", "uncertain"])
        # The schema factory must not leak mutations to later calls.
        option_schema["properties"]["A"]["required"].clear()
        self.assertEqual(
            tagged_review_schema()["properties"]["option_assessments"]
            ["properties"]["A"]["required"], ["status", "rationale"],
        )

    def test_canonical_tags_are_required_and_stable_across_languages(self):
        self.assertEqual(candidate().fact.key, candidate(language="German").fact.key)
        invalid = candidate().model_dump()
        invalid["tags"]["entity"] = ["Albert Einstein"]
        with self.assertRaises(ValidationError):
            TaggedQuestion.model_validate(invalid)
        invalid = candidate().model_dump()
        invalid["tags"]["entity"] = ["someone-else"]
        with self.assertRaises(ValidationError):
            TaggedQuestion.model_validate(invalid)
        invalid = candidate().model_dump()
        del invalid["fact"]
        with self.assertRaises(ValidationError):
            TaggedQuestion.model_validate(invalid)

    def test_same_fact_is_rejected_even_with_different_wording_or_language(self):
        previous = HistoryItem(
            question_id="previous", category_slug="history",
            question_text=candidate().question, fact_key=candidate().fact.key,
            subject="albert-einstein", relation="birth-year", object="1879",
            tags={"entity": ("albert-einstein",), "concept": ("birth-date",),
                  "keyword": ("physicist",)},
        )
        with self.assertRaises(DuplicateQuestionError):
            require_unique(candidate(language="German"), [previous])
        # Another fact about the same person is not a duplicate.
        other = candidate(relation="nobel-prize-reason", concept="scientific-awards",
                          answer="photoelectric-effect")
        self.assertLess(similarity(other, previous).score, threshold())
        require_unique(other, [previous])


    def test_literal_1984_repeat_rejected_even_when_fact_direction_and_answers_change(self):
        first = TaggedQuestion.model_validate({
            "question": 'Кто написал роман «1984»?', "difficulty": 2,
            "answers": {"A": "Джордж Оруэлл", "B": "Олдос Хаксли",
                        "C": "Рэй Брэдбери", "D": "Герберт Уэллс"},
            "correct_answer": "A",
            "tags": {"entity": ["george-orwell", "aldous-huxley", "ray-bradbury"],
                     "concept": ["authors", "novels"], "keyword": ["1984"]},
            "fact": {"subject": "george-orwell", "relation": "author-of",
                     "object": "nineteen-eighty-four"},
        })
        same_words = TaggedQuestion.model_validate({
            "question": '  КТО  написал роман "1984" ?  ', "difficulty": 2,
            "answers": {"A": "Олдос Хаксли", "B": "Джордж Оруэлл",
                        "C": "Рэй Брэдбери", "D": "Грэм Грин"},
            "correct_answer": "B",
            "tags": {"entity": ["nineteen-eighty-four", "george-orwell"],
                     "concept": ["author", "literature"], "keyword": ["dystopia"]},
            "fact": {"subject": "nineteen-eighty-four", "relation": "author",
                     "object": "george-orwell"},
        })
        history = HistoryItem(
            question_id="the-original-1984", category_slug="literature",
            question_text=first.question, fact_key=first.fact.key,
            subject=first.fact.subject, relation=first.fact.relation,
            object=first.fact.object,
            tags={kind: tuple(values) for kind, values in first.tags.model_dump().items()},
        )
        match = similarity(same_words, history)
        # New work-centered identity matches even if the fact direction was
        # inverted by older models. This is now a SAME FACT, not merely a
        # same-text fallback.
        self.assertEqual((match.score, match.reason, match.previous_id),
                         (100, "same_fact", "the-original-1984"))
        with self.assertRaises(DuplicateQuestionError) as rejected:
            require_unique(same_words, [history])
        self.assertEqual(rejected.exception.previous_id, "the-original-1984")

    def test_distinct_works_by_same_author_do_not_collide(self):
        history = HistoryItem(
            question_id="mona", category_slug="art",
            question_text='Кто написал картину «Мона Лиза»?',
            fact_key="mona-lisa-historical-key",
            subject="mona-lisa", relation="painter", object="leonardo-da-vinci",
            tags={"entity": ("mona-lisa", "leonardo-da-vinci"),
                  "concept": ("painting-authorship", "renaissance"),
                  "keyword": ("artist",)},
        )
        candidate_question = TaggedQuestion.model_validate({
            "question": 'Кто создал «Тайную вечерю»?', "difficulty": 0,
            "answers": {"A": "Микеланджело", "B": "Леонардо да Винчи",
                        "C": "Рафаэль", "D": "Боттичелли"},
            "correct_answer": "B",
            "tags": {"entity": ["the-last-supper", "leonardo-da-vinci"],
                     "concept": ["painting-authorship", "renaissance"],
                     "keyword": ["art"]},
            "fact": {"subject": "the-last-supper", "relation": "painter",
                     "object": "leonardo-da-vinci"},
        })
        match = similarity(candidate_question, history)
        self.assertEqual((match.score, match.reason), (40, "entity+concept"))
        require_unique(candidate_question, [history])

    def test_real_lermontov_log_distinct_poems_do_not_collide(self):
        # 2026-11-02: old key of author + wrote-poem was the SAME for
        # Borodino and Parus. Three retries exhausted at level 2.
        from tagging.contract import Fact
        from tagging.core import generator_context
        author, relation = "mikhail-lermontov", "wrote-poem"

        def tagged_poem(work: str, text: str) -> TaggedQuestion:
            return TaggedQuestion.model_validate({
                "difficulty": 2, "question": text,
                "answers": {"A": "Александр Пушкин", "B": "Михаил Лермонтов",
                            "C": "Фёдор Тютчев", "D": "Николай Некрасов"},
                "correct_answer": "B",
                "tags": {
                    "entity": [author, work],
                    "concept": ["poem-authorship", "russian-poetry"],
                    "keyword": ["author"],
                },
                "fact": {"subject": author, "relation": relation, "object": work},
            })

        borodino = tagged_poem("borodino", 'Кто написал стихотворение «Бородино»?')
        parus = tagged_poem("parus", 'Кто написал стихотворение «Парус»?')
        self.assertEqual(borodino.fact.legacy_key, parus.fact.legacy_key)
        self.assertNotEqual(borodino.fact.key, parus.fact.key)

        # Simulate the user's existing historical DB record, not a rekey.
        prior = HistoryItem(
            question_id="historical-borodino", category_slug="literature",
            question_text=borodino.question, fact_key=borodino.fact.legacy_key,
            subject=author, relation=relation, object="borodino",
            tags={kind: tuple(values)
                  for kind, values in borodino.tags.model_dump().items()},
        )
        match = similarity(parus, prior)
        self.assertEqual(match.score, 40)
        self.assertEqual(match.reason, "entity+concept")
        require_unique(parus, [prior])
        self.assertEqual(generator_context([prior], "literature")[0]["object"],
                         "borodino")

        # The SAME work remains a duplicate under old keys, even if worded
        # differently and options are permuted.
        repeated = tagged_poem("borodino", "Автор поэмы «Бородино» — кто?")
        self.assertEqual(similarity(repeated, prior).reason, "same_fact")
        with self.assertRaises(DuplicateQuestionError):
            require_unique(repeated, [prior])

        # Modern orientation: the work, not its author, is the subject.
        work_first = TaggedQuestion.model_validate({
            **parus.model_dump(),
            "tags": {
                "entity": ["parus", author],
                "concept": ["poem-authorship"], "keyword": ["author"],
            },
            "fact": {
                "subject": "parus", "relation": "author",
                "object": author,
            },
            "question": 'Кем написан «Парус»?',
        })
        self.assertEqual(work_first.fact.key, parus.fact.key)
        self.assertNotEqual(work_first.fact.key, borodino.fact.key)
        with self.assertRaises(DuplicateQuestionError):
            require_unique(work_first, [HistoryItem(
                question_id="earlier-parus", category_slug="literature",
                question_text=parus.question, fact_key=parus.fact.legacy_key,
                subject=author, relation=relation, object="parus",
                tags={kind: tuple(values)
                      for kind, values in parus.tags.model_dump().items()},
            )])

    def test_different_creative_works_do_not_gain_wording_points(self):
        from tagging.contract import Fact
        poem_a = Fact(subject="mikhail-lermontov", relation="wrote-poem",
                      object="borodino")
        poem_b = Fact(subject="mikhail-lermontov", relation="wrote-poem",
                      object="parus")
        self.assertNotEqual(poem_a.key, poem_b.key)
        # By contrast, stable one-to-one facts exclude the potentially
        # wrong answer object to detect changed-key duplicates.
        year_a = Fact(subject="albert-einstein", relation="birth-year",
                      object="1879")
        year_b = Fact(subject="albert-einstein", relation="birth-year",
                      object="1880")
        self.assertEqual(year_a.key, year_b.key)

    def test_exact_text_comparison_keeps_numeric_operators_and_decimals(self):
        from tagging.core import _exact_question
        self.assertNotEqual(_exact_question("Значение при x=-2?"),
                            _exact_question("Значение при x=2?"))
        self.assertNotEqual(_exact_question("Сколько будет 1.5?"),
                            _exact_question("Сколько будет 15?"))
        self.assertEqual(_exact_question(' Кто написал роман «1984»? '),
                         _exact_question('кто написал роман "1984" ?'))

    def test_weight_threshold_rejects_near_identical_questions(self):
        original = candidate()
        old = HistoryItem(
            question_id="previous", category_slug="history",
            question_text=original.question,
            fact_key="another-fact-key", subject=original.fact.subject,
            relation="other-relation", object=original.fact.object,
            tags={"entity": tuple(original.tags.entity),
                  "concept": tuple(original.tags.concept),
                  "keyword": tuple(original.tags.keyword)},
        )
        self.assertGreaterEqual(rank(original, [old])[0].score, 70)
        with self.assertRaises(DuplicateQuestionError):
            require_unique(original, [old])
        with patch.dict(os.environ, {"QUIZ_TAG_DUPLICATE_SCORE_THRESHOLD": "101"}):
            with self.assertRaises(ValueError):
                threshold()


class TaggedPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def fk_on(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
        a = User(username="Alice", username_key="alice")
        b = User(username="Bob", username_key="bob")
        topic = Topic(slug=category_slug("History"), labels={"en": "History"})
        self.db.add_all([a, b, topic])
        self.db.commit()
        self.alice, self.bob = a.id, b.id
        self.first_run = create_game(self.db, user_id=self.alice, language="English")

    def offer(self, run, user, item):
        return offer_question(
            self.db, run_id=run.id, user_id=user, question=item, category="History",
            provider="test", model="stub", schema_version="question_tagged_v2",
            dedup_enforce=True,
        )

    def test_metadata_and_attempt_are_saved_together(self):
        attempt, stored = self.offer(self.first_run, self.alice, candidate())
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionFact)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Tag)), 3)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionTag)), 3)
        history = history_for_user(self.db, self.alice)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].fact_key, candidate().fact.key)
        self.assertEqual(history[0].tags["entity"], ("albert-einstein",))
        self.assertEqual(attempt.question_id, stored.id)
        self.assertIsNone(attempt.answered_at)  # Even unanswered questions count.


    def test_accepted_tagged_critic_report_persisted_in_same_transaction(self):
        prepared = candidate()
        prepared._review_report = {
            "checker_type": "ai_tagged_critic",
            "provider": "openai",
            "model": "stub",
            "contract_version": "tagged_review_v4",
            "decision": "audit",
            "issues": [],
            "details": {
                "general_mode": "audit",
                "requested_difficulty": 0,
                "verdict": {
                    "difficulty": "appropriate",
                    "wording_penalty": 1,
                    "rationale": "Unnatural wording",
                },
                "assessment": {
                    "total": 1,
                    "penalties": {
                        "distractors": 0, "knowledge": 0,
                        "suitability": 0, "wording": 1,
                    },
                },
            },
            "duration_ms": 1250,
        }
        attempt, stored = self.offer(self.first_run, self.alice, prepared)
        saved = self.db.scalar(
            select(QuestionReview).where(QuestionReview.question_id == stored.id)
        )
        self.assertIsNotNone(saved)
        self.assertEqual(saved.contract_version, "tagged_review_v4")
        self.assertEqual(saved.decision, "audit")
        self.assertEqual(saved.details["assessment"]["penalties"]["wording"], 1)
        self.assertEqual(saved.details["verdict"]["rationale"], "Unnatural wording")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionAttempt)), 1)
        self.assertNotIn("_review_report", prepared.model_dump())


    def test_generation_critic_and_database_review_chain(self):
        from api import generate_valid_question
        mock_verdict = {
            "facts": "pass", "options": "pass", "language": "pass", "leakage": "pass",
            "difficulty": "too_easy", "independent_answer": "B", "issue_codes": [],
            "distractor_penalty": 1, "knowledge_penalty": 1,
            "suitability_penalty": 0, "wording_penalty": 1,
            "rationale": "The question is simple and awkwardly worded.",
            "tags_valid": "pass", "fact_valid": "pass",
            "semantic_duplicate": False, "duplicate_match": "",
            "premise": "valid",
            "option_assessments": {
                k: {"status": "yes" if k == "B" else "no",
                    "rationale": f"Validated {k} against the literal stem."}
                for k in "ABCD"
            },
        }
        with patch.dict(os.environ, {"AI_QUALITY_MODE": "audit",
                                      "AI_LOCAL_LEAK_MODE": "off"}), \
             patch("api.create_question", return_value=candidate().model_dump()), \
             patch("tagging.review.ReviewerSettings.from_env",
                   return_value=Mock(provider="openai", model_id="stub",
                                     structured_output_mode="strict")), \
             patch("tagging.review.LLMClient"), \
             patch("tagging.review.completed_text",
                   return_value=json.dumps(mock_verdict)):
            generated = generate_valid_question(
                0, "English", "History", tag_history=[], tag_context=[]
            )
        _, saved_question = self.offer(self.first_run, self.alice, generated)
        review = self.db.scalar(select(QuestionReview).where(
            QuestionReview.question_id == saved_question.id
        ))
        self.assertIsNotNone(review)
        self.assertEqual(review.checker_type, "ai_tagged_critic")
        self.assertEqual(review.provider, "openai")
        self.assertEqual(review.contract_version, "tagged_review_v5")
        self.assertEqual(review.details["verdict"]["premise"], "valid")
        self.assertEqual(
            review.details["verdict"]["option_assessments"]["B"]["status"], "yes",
        )
        self.assertEqual(review.details["option_diagnostics"]["outcome"], "single_correct")
        self.assertEqual(review.details["option_diagnostics"]["consistency_flags"], [])
        self.assertEqual(review.details["assessment"]["total"], 1.75)
        self.assertEqual(review.details["python_duplicate_check"]["score"], 0)
        self.assertEqual(review.details["python_duplicate_check"]["threshold"], 70)
        self.assertEqual(review.details["verdict"]["difficulty"], "too_easy")
        self.assertEqual(review.details["verdict"]["rationale"],
                         "The question is simple and awkwardly worded.")
        self.assertEqual(review.decision, "audit")

    def test_no_duplicate_for_same_user_across_games_or_languages(self):
        self.offer(self.first_run, self.alice, candidate())
        new_run = create_game(self.db, user_id=self.alice, language="German")
        with self.assertRaises(DuplicateQuestionError):
            self.offer(new_run, self.alice, candidate(language="German"))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionAttempt)), 1)

    def test_same_fact_allowed_for_another_player(self):
        self.offer(self.first_run, self.alice, candidate())
        other = create_game(self.db, user_id=self.bob, language="German")
        self.offer(other, self.bob, candidate(language="German"))
        self.assertEqual(len(history_for_user(self.db, self.bob)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionAttempt)), 2)

    def test_untagged_historical_question_fails_closed(self):
        plain = QuizQuestion.model_validate({
            key: value for key, value in candidate().model_dump().items()
            if key not in ("tags", "fact")
        })
        legacy_run = create_game(self.db, user_id=self.bob, language="English")
        offer_question(
            self.db, run_id=legacy_run.id, user_id=self.bob, question=plain,
            category="History", provider="test", model="stub",
        )
        with self.assertRaises(HistoryNotTagged):
            history_for_user(self.db, self.bob)
        new_run = create_game(self.db, user_id=self.bob, language="German")
        with self.assertRaises(HistoryNotTagged):
            self.offer(new_run, self.bob, candidate(language="German"))

    def test_different_facts_about_same_entity_are_allowed(self):
        self.offer(self.first_run, self.alice, candidate())
        another = create_game(self.db, user_id=self.alice, language="English")
        other_fact = candidate(relation="nobel-prize-reason",
                               concept="scientific-awards", answer="photoelectric-effect")
        self.offer(another, self.alice, other_fact)
        self.assertEqual(len(history_for_user(self.db, self.alice)), 2)


    def test_postgres_history_legacy_author_key_survives_and_allows_other_work(self):
        # In-place compatibility: legacy DB hashes are NOT rewritten.
        from tagging.contract import Fact

        def poem(slug, title):
            # Each new test game begins at difficulty zero; the candidate
            # text/fact still reproduces the level-2 production collision.
            return TaggedQuestion.model_validate({
                "difficulty": 0,
                "question": f'Кто написал стихотворение «{title}»?',
                "answers": {"A": "Александр Пушкин",
                            "B": "Михаил Лермонтов",
                            "C": "Фёдор Тютчев", "D": "Николай Некрасов"},
                "correct_answer": "B",
                "tags": {
                    "entity": ["mikhail-lermontov", slug],
                    "concept": ["poem-authorship"], "keyword": [],
                },
                "fact": {
                    "subject": "mikhail-lermontov",
                    "relation": "wrote-poem", "object": slug,
                },
            })

        original = poem("borodino", "Бородино")
        _, stored = self.offer(self.first_run, self.alice, original)
        persisted = self.db.get(QuestionFact, stored.id)
        persisted.fact_key = original.fact.legacy_key
        self.db.commit()

        historical = history_for_user(self.db, self.alice)
        self.assertEqual(historical[0].fact_key, original.fact.legacy_key)
        # Resumable backfill must not reject a record it originally wrote.
        save_from_repo = __import__(
            "tagging.repository", fromlist=["save_metadata"]
        ).save_metadata
        save_from_repo(self.db, stored, original, origin="backfill")
        self.db.flush()
        self.assertEqual(self.db.get(QuestionFact, stored.id).fact_key,
                         original.fact.legacy_key)

        second = poem("parus", "Парус")
        another_run = create_game(self.db, user_id=self.alice, language="Russian")
        _, saved_second = self.offer(another_run, self.alice, second)
        self.assertNotEqual(stored.id, saved_second.id)
        self.assertEqual(len(history_for_user(self.db, self.alice)), 2)
        self.assertEqual(
            self.db.get(QuestionFact, saved_second.id).fact_key, second.fact.key
        )

    def test_backfill_dry_run_never_calls_ai_or_mutates_db(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from tagging import backfill
        plain = QuizQuestion.model_validate({
            key: value for key, value in candidate().model_dump().items()
            if key not in ("tags", "fact")
        })
        offer_question(self.db, run_id=self.first_run.id, user_id=self.alice,
                       question=plain, category="History", provider="test", model="stub")
        self.db.rollback()
        output = StringIO()
        with patch("tagging.backfill.engine", return_value=self.engine), \
             patch("tagging.backfill.LLMClient") as ai, \
             patch("sys.argv", ["backfill", "--dry-run"]), \
             redirect_stdout(output):
            backfill.main()
        ai.assert_not_called()
        self.assertIn("Historical questions missing canonical tags/facts: 1", output.getvalue())
        self.assertIn("DRY RUN", output.getvalue())
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionFact)), 0)



    def test_historical_backfill_apply_is_resumable_and_unlocks_history(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from tagging import backfill
        plain = QuizQuestion.model_validate({
            key: value for key, value in candidate().model_dump().items()
            if key not in ("tags", "fact")
        })
        offer_question(self.db, run_id=self.first_run.id, user_id=self.alice,
                       question=plain, category="History", provider="test", model="stub")
        with self.assertRaises(HistoryNotTagged):
            history_for_user(self.db, self.alice)
        self.db.rollback()
        with patch("tagging.backfill.engine", return_value=self.engine), \
             patch("tagging.backfill._extract", return_value=candidate()) as extractor, \
             patch("sys.argv", ["backfill", "--apply", "--limit", "1"]), \
             redirect_stdout(StringIO()):
            backfill.main()
        extractor.assert_called_once()
        self.db.expire_all()
        self.assertEqual(len(history_for_user(self.db, self.alice)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionFact)), 1)
        self.db.rollback()
        # Already processed metadata is left in place on a subsequent run.
        with patch("tagging.backfill.engine", return_value=self.engine), \
             patch("tagging.backfill._extract") as extractor, \
             patch("sys.argv", ["backfill", "--apply", "--limit", "1"]), \
             redirect_stdout(StringIO()):
            backfill.main()
        extractor.assert_not_called()



    def test_backfill_repairs_missing_tag_links_without_deleting_existing_fact(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from sqlalchemy import delete
        from tagging import backfill
        _, stored = self.offer(self.first_run, self.alice, candidate())
        previous = self.db.get(QuestionFact, stored.id).fact_key
        self.db.execute(delete(QuestionTag))
        self.db.commit()
        with self.assertRaises(HistoryNotTagged):
            history_for_user(self.db, self.alice)
        self.db.rollback()
        with patch("tagging.backfill.engine", return_value=self.engine), \
             patch("tagging.backfill._extract", return_value=candidate()) as extractor, \
             patch("sys.argv", ["backfill", "--apply", "--limit", "1"]), \
             redirect_stdout(StringIO()):
            backfill.main()
        extractor.assert_called_once()
        self.db.expire_all()
        self.assertEqual(self.db.get(QuestionFact, stored.id).fact_key, previous)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(QuestionTag)), 3)
        self.assertEqual(len(history_for_user(self.db, self.alice)), 1)



class TaggedCriticTests(unittest.TestCase):
    def setUp(self):
        self.verdict = {
            "facts": "pass", "options": "pass", "language": "pass", "leakage": "pass",
            "difficulty": "appropriate", "independent_answer": "B", "issue_codes": [],
            "distractor_penalty": 0, "knowledge_penalty": 0,
            "suitability_penalty": 0, "wording_penalty": 0,
            "rationale": "Question, fact and tags have been checked.",
            "tags_valid": "pass", "fact_valid": "pass",
            "semantic_duplicate": False, "duplicate_match": "",
            "premise": "valid",
            "option_assessments": {
                k: {"status": "yes" if k == "B" else "no",
                    "rationale": f"Independently checked option {k}."}
                for k in "ABCD"
            },
        }

    def check(self, verdict, mode=QualityMode.OFF, *, item=None):
        with patch("tagging.review.ReviewerSettings.from_env",
                   return_value=Mock(provider="openai", model_id="stub",
                                     structured_output_mode="off")), \
             patch("tagging.review.LLMClient") as factory, \
             patch("tagging.review.completed_text", return_value=json.dumps(verdict)):
            reviewed = item if item is not None else candidate()
            check_tagged_quality(
                reviewed, language="Russian" if item is not None else "English",
                requested_difficulty=reviewed.difficulty,
                category="Entertainment" if item is not None else "History",
                neighbors=[], mode=mode,
                deadline=perf_counter() + 10, timeout_seconds=5,
            )
            self.assertEqual(factory.return_value.responses.create.call_count, 1)
            self.assertGreaterEqual(
                factory.return_value.responses.create.call_args.kwargs["max_output_tokens"],
                2400,
            )
            self.assertEqual(
                factory.return_value.responses.create.call_args.kwargs["output_contract"],
                OutputContract.TAGGED_REVIEW,
            )
            return reviewed

    def test_mandatory_critic_runs_even_if_general_quality_off(self):
        self.check(self.verdict, QualityMode.OFF)


    def test_editorial_score_breakdown_is_logged_and_report_remains_private(self):
        editorial = {
            **self.verdict,
            "difficulty": "too_easy",
            "distractor_penalty": 2,
            "knowledge_penalty": 1,
            "suitability_penalty": 1,
            "wording_penalty": 1,
            "rationale": "Too easy with awkward language.",
        }
        with patch("tagging.review.event") as log:
            checked = self.check(editorial, QualityMode.AUDIT)
        self.assertIsNotNone(checked._review_report)
        details = checked._review_report["details"]
        self.assertEqual(details["verdict"]["difficulty"], "too_easy")
        self.assertEqual(details["assessment"]["total"], 3.25)
        self.assertEqual(details["assessment"]["penalties"]["distractors"], 1)
        self.assertEqual(details["assessment"]["penalties"]["wording"], 1)
        self.assertEqual(checked._review_report["decision"], "audit")
        self.assertNotIn("_review_report", checked.model_dump())
        scores = [call.kwargs for call in log.call_args_list
                  if call.args[0] == "tagged_review_score"]
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["score"], 3.25)
        self.assertEqual(scores[0]["penalties"]["knowledge"], 0.25)
        self.assertEqual(scores[0]["difficulty"], "too_easy")
        self.assertNotIn("rationale", scores[0])

    def test_scores_are_logged_even_for_rejected_tagged_question(self):
        with patch("tagging.review.event") as log:
            with self.assertRaises(QualityRegenerationError):
                self.check({**self.verdict, "fact_valid": "fail"})
        score_events = [call for call in log.call_args_list
                        if call.args[0] == "tagged_review_score"]
        self.assertEqual(len(score_events), 1)
        self.assertEqual(score_events[0].kwargs["fact_valid"], "fail")


    def test_difficulty_weight_only_a_quarter_to_half_point(self):
        normal = tagged_assessment(TaggedReview.model_validate(self.verdict), "B")
        self.assertEqual(normal["total"], 0)
        easy = {**self.verdict, "difficulty": "too_easy", "knowledge_penalty": 0}
        minor = tagged_assessment(TaggedReview.model_validate(easy), "B")
        self.assertEqual(minor["penalties"]["knowledge"], 0.25)
        self.assertEqual(minor["total"], 0.25)
        severe = {**easy, "knowledge_penalty": 2}
        major = tagged_assessment(TaggedReview.model_validate(severe), "B")
        self.assertEqual(major["penalties"]["knowledge"], 0.5)
        self.assertEqual(major["total"], 0.5)
        self.assertEqual(major["raw_penalties"]["knowledge"], 2)

    def test_subjective_difficulty_alone_never_rejects_enforce(self):
        flagged = {
            **self.verdict, "difficulty": "too_easy",
            "knowledge_penalty": 2,
            "issue_codes": ["difficulty_mismatch"],
        }
        with patch("tagging.review.event") as event:
            verified = self.check(flagged, QualityMode.ENFORCE)
        self.assertEqual(verified._review_report["details"]["assessment"]["total"], 0.5)
        self.assertEqual(verified._review_report["decision"], "accept")
        score = [call.kwargs for call in event.call_args_list
                 if call.args[0] == "tagged_review_score"][0]
        self.assertEqual(score["score_threshold"], 3.25)
        self.assertFalse(score["threshold_preview"]["3.25"])

    def test_difficulty_quarter_tips_three_points_to_enforce_rejection(self):
        other_three = {**self.verdict, "distractor_penalty": 3}
        # An otherwise marginal question with three points is NOT rejected.
        accepted = self.check(other_three, QualityMode.ENFORCE)
        self.assertEqual(accepted._review_report["details"]["assessment"]["total"], 3)
        critical_plus_quarter = {
            **other_three, "difficulty": "too_easy", "knowledge_penalty": 0,
        }
        with self.assertRaises(QualityRegenerationError):
            self.check(critical_plus_quarter, QualityMode.ENFORCE)
        # Audit still records the exact same 3.25 without blocking.
        approved_in_audit = self.check(critical_plus_quarter, QualityMode.AUDIT)
        self.assertEqual(
            approved_in_audit._review_report["details"]["assessment"]["total"], 3.25
        )
        self.assertTrue(
            approved_in_audit._review_report["details"]["assessment"][
                "threshold_preview"]["3.25"]
        )

    def test_invalid_tags_are_rejected_even_if_general_quality_off(self):
        with self.assertRaises(QualityRegenerationError):
            self.check({**self.verdict, "tags_valid": "fail"})

    def test_facts_and_options_failures_are_diagnostic_in_audit(self):
        # The Jam / Beatles: the critic may flag quality errors, but audit
        # must not silently become enforce.
        for finding in ("facts", "options"):
            with self.subTest(finding=finding):
                verdict = {
                    **self.verdict, finding: "fail", "fact_valid": "pass",
                    "rationale": "The critic identified an incorrect premise or options.",
                }
                observed = self.check(verdict, QualityMode.AUDIT)
                self.assertEqual(observed._review_report["decision"], "audit")
                self.assertEqual(observed._review_report["details"]["assessment"]["critical"],
                                 "regenerate")
                with self.assertRaises(QualityRegenerationError):
                    self.check(verdict, QualityMode.ENFORCE)



    def test_v5_requires_complete_valid_a_to_d_evidence(self):
        bad = [
            {"premise": "bogus"},
            {"option_assessments": {"A": self.verdict["option_assessments"]["A"]}},
            {"option_assessments": {**self.verdict["option_assessments"],
                                    "E": {"status": "no", "rationale": "Extra option"}}},
            {"option_assessments": {**self.verdict["option_assessments"],
                                    "C": {"status": "maybe", "rationale": "Not sure"}}},
            {"option_assessments": {**self.verdict["option_assessments"],
                                    "C": {"status": "uncertain", "rationale": " "}}},
            {"option_assessments": {**self.verdict["option_assessments"],
                                    "D": {"status": "no", "rationale": "x" * 251}}},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                TaggedReview.model_validate({**self.verdict, **change})
        for omitted in ("premise", "option_assessments"):
            verdict = {**self.verdict}
            del verdict[omitted]
            with self.subTest(omitted=omitted), self.assertRaises(ValidationError):
                TaggedReview.model_validate(verdict)
        with self.assertRaises(ValidationError):
            TaggedReview.model_validate({**self.verdict, "unexpected": 123})

    def test_v4_model_reply_cannot_be_mistaken_for_complete_v5_evidence(self):
        # A missing new field is an invalid/incomplete review, NOT an audit
        # success and NOT a newly invented factual rejection.
        old_reply = {k: v for k, v in self.verdict.items()
                     if k not in ("premise", "option_assessments")}
        with self.assertRaises(QualityReviewFailure) as caught:
            self.check(old_reply, QualityMode.AUDIT)
        self.assertEqual(caught.exception.classification, "invalid_tag_review")

    def test_false_only_premise_is_visible_but_does_not_silently_enforce(self):
        # The Jam: old critic incorrectly says facts/options pass, while
        # the new per-option evidence exposes a false "only" premise.
        wrong_legacy_verdict = {
            **self.verdict,
            "premise": "invalid",
            "option_assessments": {
                k: {"status": "no", "rationale": "Not the ONLY UK #1 single."}
                for k in "ABCD"
            },
        }
        with patch("tagging.review.event") as event_mock:
            audit = self.check(wrong_legacy_verdict, QualityMode.AUDIT)
        details = audit._review_report["details"]
        self.assertEqual(details["option_diagnostics"]["outcome"], "invalid_premise")
        self.assertIn("invalid_premise_but_facts_pass",
                      details["option_diagnostics"]["consistency_flags"])
        self.assertEqual(details["assessment"]["critical"], "none")
        self.assertEqual(audit._review_report["decision"], "audit")
        [event_call] = [call for call in event_mock.call_args_list
                        if call.args[0] == "tagged_option_audit"]
        self.assertEqual(event_call.kwargs["statuses"], dict.fromkeys("ABCD", "no"))
        self.assertEqual(event_call.kwargs["outcome"], "invalid_premise")
        self.assertNotIn("rationale", event_call.kwargs)
        self.assertNotIn("Not the ONLY", json.dumps(event_call.kwargs))

        # Not a stealth new gate: same legacy verdict still decides enforce.
        enforce = self.check(wrong_legacy_verdict, QualityMode.ENFORCE)
        self.assertEqual(enforce._review_report["decision"], "accept")

    def test_four_yes_options_disagree_with_legacy_pass_without_blocking(self):
        four = {
            **self.verdict,
            "option_assessments": {
                k: {"status": "yes", "rationale": "Reached #1 in UK."}
                for k in "ABCD"
            },
        }
        assessed = option_evidence_diagnostics(TaggedReview.model_validate(four), "B")
        self.assertEqual(assessed["outcome"], "multiple_correct")
        self.assertEqual(assessed["supported_choices"], list("ABCD"))
        self.assertIn("nonunique_options_but_options_pass",
                      assessed["consistency_flags"])
        self.assertEqual(self.check(four, QualityMode.AUDIT)._review_report["decision"],
                         "audit")
        self.assertEqual(self.check(four, QualityMode.ENFORCE)._review_report["decision"],
                         "accept")

    def test_uncertain_option_and_key_mismatch_are_diagnostic_only(self):
        uncertain = {
            **self.verdict,
            "option_assessments": {
                **self.verdict["option_assessments"],
                "D": {"status": "uncertain", "rationale": "Unverified"},
            },
        }
        checked = self.check(uncertain, QualityMode.AUDIT)
        diagnostic = checked._review_report["details"]["option_diagnostics"]
        self.assertEqual(diagnostic["outcome"], "uncertain")
        self.assertEqual(diagnostic["uncertain_choices"], ["D"])
        self.assertIn("uncertain_choices_but_options_pass",
                      diagnostic["consistency_flags"])
        mismatch = {
            **self.verdict,
            "option_assessments": {
                k: {"status": "yes" if k == "C" else "no",
                    "rationale": "Independently assessed"}
                for k in "ABCD"
            },
        }
        result = self.check(mismatch, QualityMode.AUDIT)
        flags = result._review_report["details"]["option_diagnostics"]["consistency_flags"]
        self.assertIn("generator_key_disagrees_with_options", flags)
        self.assertIn("independent_answer_disagrees_with_options", flags)

    def test_factual_failure_still_rejects_enforce_and_audits_without_new_gate(self):
        finding = {
            **self.verdict, "facts": "fail", "options": "fail",
            "premise": "invalid",
            "option_assessments": {
                k: {"status": "no", "rationale": "Does not satisfy the stem."}
                for k in "ABCD"
            },
        }
        audited = self.check(finding, QualityMode.AUDIT)
        self.assertEqual(audited._review_report["decision"], "audit")
        self.assertEqual(
            audited._review_report["details"]["option_diagnostics"]["outcome"],
            "invalid_premise",
        )
        with self.assertRaises(QualityRegenerationError):
            self.check(finding, QualityMode.ENFORCE)

    def test_question_review_prompt_explicitly_checks_all_four_and_quantifiers(self):
        from tagging.review import TAGGED_REVIEW_PROMPT
        self.assertIn("A, B, C and D SEPARATELY", TAGGED_REVIEW_PROMPT)
        self.assertIn("candidate.correct_answer as an UNTRUSTED hypothesis", TAGGED_REVIEW_PROMPT)
        self.assertIn("One counterexample", TAGGED_REVIEW_PROMPT)
        self.assertIn("единственный", TAGGED_REVIEW_PROMPT)
        self.assertIn("facts=pass and options=pass", TAGGED_REVIEW_PROMPT)
        self.assertIn("option_assessments", TAGGED_REVIEW_PROMPT)
        self.assertIn("premise", TAGGED_REVIEW_PROMPT)
        self.assertIn("EVIDENCE ONLY", TAGGED_REVIEW_PROMPT)

    def test_gta_vice_city_leak_is_logged_in_audit_and_rejected_in_enforce(self):
        # Real level-4 regression: the critic correctly catches the giveaway.
        # Audit intentionally accepts for observation; enforce regenerates.
        verdict = {
            **self.verdict,
            "leakage": "fail",
            "issue_codes": ["answer_leak", "difficulty_mismatch"],
            "difficulty": "too_easy", "distractor_penalty": 1,
            "knowledge_penalty": 0,
            "rationale": "Vice City in the title matches Вайс-Сити.",
        }
        gta = TaggedQuestion.model_validate({
            "difficulty": 4,
            "question": ("Как называется вымышленный город, в котором происходит "
                         "действие игры Grand Theft Auto: Vice City?"),
            "answers": {
                "A": "Либерти-Сити", "B": "Вайс-Сити",
                "C": "Сан-Андреас", "D": "Лос-Сантос",
            },
            "correct_answer": "B",
            "tags": {
                "entity": ["grand-theft-auto-vice-city", "vice-city"],
                "concept": ["video-game-setting", "fictional-city", "game-location"],
                "keyword": ["gta", "setting"],
            },
            "fact": {
                "subject": "grand-theft-auto-vice-city",
                "relation": "setting-city", "object": "vice-city",
            },
        })
        assessment = tagged_assessment(TaggedReview.model_validate(verdict), gta.correct_answer)
        self.assertEqual(assessment["total"], 1.25)
        for mode in (QualityMode.OFF, QualityMode.AUDIT):
            with self.subTest(mode=mode):
                reviewed = self.check(verdict, mode, item=gta)
                report = reviewed._review_report
                self.assertEqual(report["decision"], "audit")
                self.assertEqual(report["details"]["verdict"]["leakage"], "fail")
                self.assertIn("answer_leak", report["issues"])
                self.assertEqual(report["details"]["assessment"]["critical"], "regenerate")
        with self.assertRaises(QualityRegenerationError):
            self.check(verdict, QualityMode.ENFORCE, item=gta)

    def test_answer_leak_issue_code_is_audit_only_but_blocks_enforce(self):
        verdict = {
            **self.verdict, "leakage": "pass", "issue_codes": ["answer_leak"],
        }
        observed = self.check(verdict, QualityMode.AUDIT)
        self.assertEqual(observed._review_report["decision"], "audit")
        self.assertEqual(observed._review_report["details"]["assessment"]["critical"],
                         "regenerate")
        with self.assertRaises(QualityRegenerationError):
            self.check(verdict, QualityMode.ENFORCE)

    def test_high_difficulty_penalty_without_leak_still_audit_only(self):
        verdict = {
            **self.verdict, "difficulty": "too_easy",
            "issue_codes": ["difficulty_mismatch"], "distractor_penalty": 3,
        }
        accepted = self.check(verdict, QualityMode.AUDIT)
        self.assertEqual(accepted._review_report["decision"], "audit")

    def test_semantic_duplicate_is_rejected_even_in_audit(self):
        with self.assertRaises(QualityRegenerationError):
            self.check({**self.verdict, "semantic_duplicate": True,
                        "duplicate_match": "prior-id"}, QualityMode.AUDIT)


class TaggedGenerationPolicyTests(unittest.TestCase):
    """Python rejection wins even if a mocked reviewer would approve."""

    def setUp(self):
        from test_tagged_dedup import candidate as make_candidate
        self.duplicate = make_candidate()
        self.new_fact = make_candidate(relation="nobel-prize-reason",
                                       concept="scientific-awards",
                                       answer="photoelectric-effect")
        self.history = [HistoryItem(
            question_id="old", category_slug=category_slug("History"),
            question_text=self.duplicate.question, fact_key=self.duplicate.fact.key,
            subject=self.duplicate.fact.subject, relation=self.duplicate.fact.relation,
            object=self.duplicate.fact.object,
            tags={"entity": tuple(self.duplicate.tags.entity),
                  "concept": tuple(self.duplicate.tags.concept),
                  "keyword": tuple(self.duplicate.tags.keyword)},
        )]
        self.enterContext(patch.dict(os.environ, {
            "AI_QUALITY_MODE": "off", "AI_LOCAL_LEAK_MODE": "off",
            "AI_GENERATION_MAX_ATTEMPTS": "3",
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_PROVIDER_TIMEOUT_SECONDS": "12",
            "AI_RETRY_BACKOFF_BASE_SECONDS": "0.5",
            "AI_RETRY_BACKOFF_MAX_SECONDS": "2",
        }))
        self.generate = self.enterContext(patch("api.create_question"))
        self.review = self.enterContext(patch("tagging.review.check_tagged_quality"))
        self.enterContext(patch("api.sleep"))

    def test_python_rejection_precedes_critic_and_next_retry_has_exclusion(self):
        from api import generate_valid_question
        self.generate.side_effect = [
            self.duplicate.model_dump(),
            self.new_fact.model_dump(),
        ]
        accepted = generate_valid_question(
            0, "English", "History", tag_history=self.history, tag_context=[],
        )
        self.assertEqual(accepted.fact.key, self.new_fact.fact.key)
        self.assertEqual(self.generate.call_count, 2)
        self.review.assert_called_once()
        second_context = self.generate.call_args_list[1].kwargs["tag_context"]
        self.assertEqual(second_context[-1]["subject"], "albert-einstein")
        self.assertEqual(second_context[-1]["relation"], "birth-year")

    def test_exhausted_duplicates_return_error_and_never_reach_reviewer(self):
        from api import generate_valid_question
        from fastapi import HTTPException
        self.generate.return_value = self.duplicate.model_dump()
        with self.assertRaises(HTTPException) as raised:
            generate_valid_question(0, "English", "History",
                                    tag_history=self.history, tag_context=[])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, {"code": "duplicate_exhausted"})
        self.assertEqual(self.generate.call_count, 3)
        self.review.assert_not_called()


    def test_exhausted_critic_semantic_duplicates_use_distinct_error(self):
        from api import generate_valid_question
        from fastapi import HTTPException
        from tagging.core import SemanticDuplicateError
        self.generate.return_value = self.new_fact.model_dump()
        self.review.side_effect = SemanticDuplicateError()
        with self.assertRaises(HTTPException) as raised:
            generate_valid_question(0, "English", "History",
                                    tag_history=self.history, tag_context=[])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, {"code": "duplicate_exhausted"})
        self.assertEqual(self.review.call_count, 3)

    def test_python_similarity_score_and_threshold_are_logged_before_critic(self):
        from api import generate_valid_question
        self.generate.return_value = self.new_fact.model_dump()
        with patch("api.event") as recorded:
            result = generate_valid_question(
                0, "English", "History", tag_history=self.history, tag_context=[]
            )
        events = [call.kwargs for call in recorded.call_args_list
                  if call.args[0] == "tag_similarity_score"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["score"], 20)
        self.assertEqual(events[0]["threshold"], 70)
        self.assertEqual(events[0]["reason"], "entity")
        self.assertFalse(events[0]["rejected"])
        self.assertEqual(result._duplicate_assessment["score"], 20)

    def test_rejected_duplicate_score_is_logged_with_threshold(self):
        from api import generate_valid_question
        from fastapi import HTTPException
        self.generate.return_value = self.duplicate.model_dump()
        with patch("api.event") as recorded:
            with self.assertRaises(HTTPException):
                generate_valid_question(0, "English", "History",
                                        tag_history=self.history, tag_context=[])
        events = [call.kwargs for call in recorded.call_args_list
                  if call.args[0] == "tag_similarity_score"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["score"], 100)
        self.assertEqual(events[0]["threshold"], 70)
        self.assertTrue(all(event["rejected"] for event in events))

    def test_mixed_invalid_question_and_duplicate_errors_keep_generic_failure(self):
        from api import generate_valid_question
        from fastapi import HTTPException
        self.generate.side_effect = [
            self.duplicate.model_dump(),
            {**self.new_fact.model_dump(), "difficulty": 8},
            self.duplicate.model_dump(),
        ]
        with self.assertRaises(HTTPException) as raised:
            generate_valid_question(0, "English", "History",
                                    tag_history=self.history, tag_context=[])
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "AI could not generate a valid question")
        self.review.assert_not_called()
