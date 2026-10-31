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
from quality_errors import QualityRegenerationError
from quality_schema import QualityMode
from question_schema import QuizQuestion
from tagging.contract import TaggedQuestion
from tagging.core import (
    DuplicateQuestionError, HistoryItem, HistoryNotTagged, rank, require_unique,
    similarity, threshold,
)
from tagging.repository import history_for_user
from tagging.review import check_tagged_quality
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
        self.assertEqual(transport_contract(OutputContract.TAGGED_QUESTION)[0], "quiz_question_tagged_v2")
        schema = tagged_question_schema()
        self.assertEqual(set(schema["properties"]), set(TaggedQuestion.model_fields))
        self.assertEqual(set(schema["required"]), set(TaggedQuestion.model_fields))
        self.assertEqual(set(tag_extraction_schema()["properties"]), {"tags", "fact"})
        self.assertIn("semantic_duplicate", tagged_review_schema()["required"])

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
        }

    def check(self, verdict, mode=QualityMode.OFF):
        with patch("tagging.review.ReviewerSettings.from_env",
                   return_value=Mock(provider="openai", model_id="stub",
                                     structured_output_mode="off")), \
             patch("tagging.review.LLMClient") as factory, \
             patch("tagging.review.completed_text", return_value=json.dumps(verdict)):
            reviewed = candidate()
            check_tagged_quality(
                reviewed, language="English", requested_difficulty=0,
                category="History", neighbors=[], mode=mode,
                deadline=perf_counter() + 10, timeout_seconds=5,
            )
            self.assertEqual(factory.return_value.responses.create.call_count, 1)
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
        self.assertEqual(details["assessment"]["total"], 5)
        self.assertEqual(details["assessment"]["penalties"]["distractors"], 2)
        self.assertEqual(details["assessment"]["penalties"]["wording"], 1)
        self.assertEqual(checked._review_report["decision"], "audit")
        self.assertNotIn("_review_report", checked.model_dump())
        scores = [call.kwargs for call in log.call_args_list
                  if call.args[0] == "tagged_review_score"]
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["score"], 5)
        self.assertEqual(scores[0]["penalties"]["knowledge"], 1)
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

    def test_invalid_tags_are_rejected_even_if_general_quality_off(self):
        with self.assertRaises(QualityRegenerationError):
            self.check({**self.verdict, "tags_valid": "fail"})

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
