"""Local-development FastAPI backend for AI Quiz Game.

Sessions are intentionally in memory for the first playable web prototype.
Do not deploy publicly before persistent storage, authentication and quotas.
"""

import hashlib
import json
import random
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter, sleep
from uuid import uuid4

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ValidationError

load_dotenv(find_dotenv())

from consts import LANGUAGE_DICT, QUESTION_CATEGORIES  # noqa: E402
from game_logic import GameState  # noqa: E402
from local_leak_filter import check_local_leak, local_leak_mode_from_env  # noqa: E402
from generation_policy import (GenerationPolicy, InvalidQuestionError, client_settings,
                               is_transient, provider_timeout, retry_after_seconds)  # noqa: E402
from diagnostics import elapsed_ms, event, generation_attempt, request_id, timed_stage  # noqa: E402
from quality_checker import check_quality, quality_mode_from_env  # noqa: E402
from quality_errors import (QualityArbitrationUnavailable,
                            QualityConfigurationError,
                            QualityRegenerationError)  # noqa: E402
from question_generator import create_question  # noqa: E402
from question_schema import Choice, PublicQuestion, QuizQuestion  # noqa: E402
from persistence.auth import router as auth_router  # noqa: E402
from persistence.analytics import router as profile_router  # noqa: E402
from localization.routes import router as localization_router  # noqa: E402
from localization.dev_tools import router as localization_dev_router  # noqa: E402

app = FastAPI(title="AI Quiz Game", version="0.1.0")
app.include_router(auth_router)  # Accounts are DB-backed; game sessions still in memory.
app.include_router(profile_router)  # Derived statistics are initially empty until game DB migration.
app.include_router(localization_router)
app.include_router(localization_dev_router)


@app.middleware("http")
async def question_diagnostics(request: Request, call_next):
    parts = request.url.path.split("/")
    if not (request.method == "GET" and (
            (len(parts) == 5 and parts[1:3] == ["api", "games"] and parts[4] == "question")
            or (len(parts) == 6 and parts[1:4] == ["api", "v2", "games"] and parts[5] == "question")
    )):
        return await call_next(request)
    diagnostic_id = str(uuid4())
    token = request_id.set(diagnostic_id)
    start = perf_counter()
    status = 500
    event("question_request_received")
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Diagnostic-Request-ID"] = diagnostic_id
        return response
    except Exception as exc:
        event("question_request_failed", error_type=type(exc).__name__)
        raise
    finally:
        event("question_request_completed", duration_ms=elapsed_ms(start),
              status_code=status, outcome="success" if status < 400 else "failure")
        request_id.reset(token)


class NewGame(BaseModel):
    language: str = "English"


class AnswerInput(BaseModel):
    question_id: str
    answer: Choice


class GameInfo(BaseModel):
    game_id: str
    language: str
    phase: str
    correct_main_answers: int


class AnswerFeedback(BaseModel):
    correct: bool
    correct_answer: Choice
    phase: str
    correct_main_answers: int
    achievement: str | None = None


@dataclass
class Session:
    language: str
    state: GameState = field(default_factory=GameState)
    categories: list[str] = field(default_factory=lambda: random.sample(QUESTION_CATEGORIES, len(QUESTION_CATEGORIES)))
    question: QuizQuestion | None = None
    question_id: str | None = None
    lock: Lock = field(default_factory=Lock)


# Replaced with PostgreSQL/session storage in roadmap phase 3.
sessions: dict[str, Session] = {}
sessions_lock = Lock()
MAX_SESSIONS = 200  # Local safety limit; not a substitute for authentication or quotas.


def find_session(game_id: str) -> Session:
    with sessions_lock:
        session = sessions.get(game_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Game not found")
    return session


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/client-config")
def public_client_config() -> dict[str, int]:
    try:
        return client_settings()
    except ValueError:
        raise HTTPException(status_code=503, detail="Invalid client timing configuration") from None


@app.post("/api/games", response_model=GameInfo, status_code=201)
def new_game(payload: NewGame) -> GameInfo:
    if payload.language not in LANGUAGE_DICT.values():
        raise HTTPException(status_code=422, detail="Unsupported language")
    game_id = str(uuid4())
    with sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            raise HTTPException(status_code=503, detail="Local session limit reached")
        sessions[game_id] = Session(language=payload.language)
    return GameInfo(game_id=game_id, language=payload.language, phase="main", correct_main_answers=0)


@app.get("/api/games/{game_id}", response_model=GameInfo)
def game_info(game_id: str) -> GameInfo:
    session = find_session(game_id)
    with session.lock:
        return GameInfo(game_id=game_id, language=session.language,
                        phase=session.state.phase,
                        correct_main_answers=session.state.correct_main_answers)


from tagging.core import DuplicateQuestionError, SemanticDuplicateError
from tagging.debug import debug_accepted_candidate, debug_rejected_candidate


def generate_valid_question(difficulty: int, language: str, category: str, *,
                            tag_history=None, tag_context=None,
                            too_easy_reserve: list | None = None) -> QuizQuestion:
    """Run the shared bounded AI generation, validation, leak and quality policy."""
    try:
        policy = GenerationPolicy.from_env()
        quality_mode = quality_mode_from_env()
        leak_mode = local_leak_mode_from_env()
    except (ValueError, QualityConfigurationError):
        event("generation_configuration_failed")
        raise HTTPException(status_code=503, detail="AI provider unavailable") from None
    # No new behavior for legacy games. Tagged PostgreSQL games pass an
    # explicit history list (empty for new users); never pass private history
    # or metadata to the player-facing response.
    tagged = tag_history is not None
    if tagged:
        from tagging.contract import TaggedQuestion
        from tagging.core import require_unique, critic_neighbors, threshold
        from tagging.review import check_tagged_quality, is_strict_too_easy_reserve
        from tagging.quality_skill.fact_tool import known_false_fact
        from tagging.repair import repair_four_entity_tags, validation_retry_hint
        from persistence.seed_topics import category_slug as topic_slug
        exclusions = list(tag_context or [])
    deadline = perf_counter() + policy.budget_seconds
    duplicate_rejections = 0
    other_failures = 0
    retry_feedback = None  # Static server-authored guidance; never raw error text.
    invalid_signatures: set[str] = set()  # Request-local; no user history stored.

    def failure(fallback_status: int, fallback_detail: str) -> HTTPException:
        # Only an exclusively duplicate-related exhaustion earns this code.
        # Generic provider/validation failures must not blame user history.
        if tagged and duplicate_rejections > 0 and other_failures == 0:
            return HTTPException(status_code=409, detail={"code": "duplicate_exhausted"})
        return HTTPException(status_code=fallback_status, detail=fallback_detail)

    event("generation_policy", budget_ms=policy.budget_seconds * 1000,
          max_attempts=policy.max_attempts,
          provider_timeout_ms=policy.provider_timeout_seconds * 1000, sdk_max_retries=0)
    for attempt in range(1, policy.max_attempts + 1):
        remaining = deadline - perf_counter()
        if remaining <= 0:
            event("generation_budget_exhausted")
            raise failure(503, "AI provider unavailable")
        token = generation_attempt.set(attempt)
        timeout_token = provider_timeout.set(min(policy.provider_timeout_seconds, remaining))
        event("generation_attempt_started")
        failure_status = 503
        delay_hint = None
        raw = None
        question = None
        rejection_reason = None
        rejection_error_type = None
        duplicate_reason = None
        shadow_result = None
        try:
            raw = (create_question(
                difficulty, language, category, tag_context=exclusions,
                **({"retry_feedback": retry_feedback} if retry_feedback else {}),
            ) if tagged else create_question(difficulty, language, category))
            with timed_stage("question_validation"):
                if tagged:
                    try:
                        question = TaggedQuestion.model_validate(raw)
                    except ValidationError as validation_error:
                        corrected = repair_four_entity_tags(raw, validation_error)
                        if corrected is None:
                            raise
                        # A second complete validation is mandatory. If it
                        # fails, the attempt is rejected as usual; never
                        # mutate the original raw provider response.
                        question = TaggedQuestion.model_validate(corrected)
                        event("tagged_metadata_repaired", repair="four_entity_overflow",
                              previous_count=4, current_count=3,
                              fact_subject_preserved=True)
                else:
                    question = QuizQuestion.model_validate(raw)
                if question.difficulty != difficulty:
                    raise InvalidQuestionError("AI returned the wrong difficulty")
            # Sourced exact contradictions are hard failures even if the paid
            # critic would incorrectly pass them. Unverified fact-tool
            # heuristics remain shadow-only and NEVER block publication.
            if tagged:
                contradiction = known_false_fact(question)
                if contradiction is not None:
                    event("known_fact_contradiction_blocked",
                          code=contradiction["code"])
                    raise QualityRegenerationError()
            # Strict deterministic Python check runs BEFORE the paid critic.
            if tagged:
                try:
                    closest = require_unique(question, tag_history)
                except DuplicateQuestionError as duplicate:
                    # The private DEBUG trace can identify the exact prior
                    # question; INFO events do not expose user history IDs.
                    question._duplicate_assessment = {
                        "score": duplicate.score,
                        "threshold": threshold(),
                        "reason": str(duplicate),
                        "matched_question_id": duplicate.previous_id,
                    }
                    event("tag_similarity_score", score=duplicate.score,
                          threshold=threshold(), reason=str(duplicate),
                          rejected=True)
                    raise
                question._duplicate_assessment = {
                    "score": closest.score if closest else 0,
                    "threshold": threshold(),
                    "reason": closest.reason if closest else "no_history",
                    "matched_question_id": closest.previous_id if closest else None,
                }
                event("tag_similarity_score",
                      score=question._duplicate_assessment["score"],
                      threshold=question._duplicate_assessment["threshold"],
                      reason=question._duplicate_assessment["reason"],
                      rejected=False)
            if tagged:
                # Local-only diagnostics: NEVER fail or slow an otherwise valid
                # candidate if the experimental shadow checker has a bug.
                from tagging.quality_skill import evaluate_shadow
                try:
                    shadow_result = evaluate_shadow(question, requested_difficulty=difficulty)
                except Exception as exc:
                    event("quality_skill_shadow_failed", error_type=type(exc).__name__)
            check_local_leak(question, leak_mode)
            if tagged:
                check_tagged_quality(question, requested_difficulty=difficulty,
                                     language=language, category=category,
                                     neighbors=critic_neighbors(question, tag_history,
                                                                 category_slug=topic_slug(category)),
                                     mode=quality_mode, deadline=deadline,
                                     timeout_seconds=policy.provider_timeout_seconds,
                                     clock=perf_counter, shadow=shadow_result)
            else:
                check_quality(question, difficulty, language, quality_mode,
                              deadline, policy.provider_timeout_seconds, clock=perf_counter)
            # Include successful candidates as well as rejections in the
            # opt-in DEBUG trace, with the exact critic verdict and answer.
            # This is pre-persistence acceptance, not a claim of DB commit.
            debug_accepted_candidate(question)
            event("generation_attempt_completed", outcome="success")
            break
        except QualityArbitrationUnavailable as exc:
            # A provider timeout/invalid arbiter response is NOT a failed
            # quality judgment. Re-running generator and critic on a new
            # question only wastes tokens and repeats the same outage.
            rejection_reason = "arbitration_unavailable"
            rejection_error_type = type(exc).__name__
            event("generation_attempt_completed",
                  outcome="arbitration_unavailable", reason=exc.reason)
            event("generation_terminated",
                  reason="arbiter_unavailable_no_regeneration",
                  attempts=attempt)
            raise HTTPException(
                status_code=503, detail="Question validation temporarily unavailable"
            ) from None
        except (DuplicateQuestionError, SemanticDuplicateError) as exc:
            retry_feedback = None
            rejection_reason = "duplicate_rejected"
            rejection_error_type = type(exc).__name__
            if isinstance(exc, DuplicateQuestionError):
                duplicate_reason = str(exc)
            duplicate_rejections += 1
            failure_status = 502
            event("generation_attempt_completed", outcome="duplicate_rejected")
            # Give the next attempt a concrete additional exclusion.
            exclusions.append({"subject": question.fact.subject,
                               "relation": question.fact.relation,
                               "object": question.fact.object,
                               "rejected": True})
        except QualityRegenerationError as exc:
            retry_feedback = None
            rejection_reason = "quality_rejected"
            rejection_error_type = type(exc).__name__
            other_failures += 1
            failure_status = 502
            if (tagged and difficulty < 15 and too_easy_reserve is not None
                    and question is not None and is_strict_too_easy_reserve(question)):
                # Keep only a fully checked, strictly too-easy-only question;
                # publication is a separate last resort AFTER the global pool.
                too_easy_reserve.append(question.model_copy(deep=True))
                event("question_too_easy_reserve", outcome="eligible")
            if tagged:
                exclusions.append({"subject": question.fact.subject,
                                   "relation": question.fact.relation,
                                   "object": question.fact.object,
                                   "rejected": True})
            event("generation_attempt_completed", outcome="quality_rejected")
        except (json.JSONDecodeError, ValidationError, InvalidQuestionError) as exc:
            if tagged:
                retry_feedback = validation_retry_hint(exc)
                # Do not log the hint or raw Pydantic messages; they may
                # include candidate data. Only classify this retry.
                event("tagged_validation_retry_feedback",
                      kind="validation" if isinstance(exc, ValidationError)
                      else "difficulty" if isinstance(exc, InvalidQuestionError)
                      else "json")
            rejection_reason = "invalid_question"
            rejection_error_type = type(exc).__name__
            other_failures += 1
            failure_status = 502
            event("generation_attempt_completed", outcome="invalid_question",
                  error_type=type(exc).__name__)
            if tagged and isinstance(raw, (dict, list)):
                # A second byte-for-byte equivalent invalid candidate after
                # explicit retry feedback is strong evidence that another
                # paid call will repeat the same mistake. Stop early and let
                # the existing approved pool / strict reserve handle it.
                signature = hashlib.sha256(json.dumps(
                    raw, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                if signature in invalid_signatures:
                    event("generation_repeated_invalid_stopped",
                          attempt=attempt, error_type=type(exc).__name__)
                    event("generation_retries_exhausted",
                          attempts=attempt, reason="repeated_invalid")
                    raise failure(502, "AI could not generate a valid question")
                invalid_signatures.add(signature)
        except Exception as exc:
            retry_feedback = None
            rejection_reason = "provider_failure"
            rejection_error_type = type(exc).__name__
            other_failures += 1
            retryable = is_transient(exc)
            event("generation_attempt_completed", outcome="provider_failure",
                  error_type=type(exc).__name__, retryable=retryable)
            if not retryable:
                raise HTTPException(status_code=503, detail="AI provider unavailable") from None
            delay_hint = retry_after_seconds(exc)
        finally:
            # Attach the current request/attempt context before resetting it.
            # No candidate bodies, answers, or critic rationales reach INFO logs.
            if rejection_reason and (question is not None or raw is not None):
                debug_rejected_candidate(
                    question if question is not None else raw,
                    reason=rejection_reason,
                    error_type=rejection_error_type or "Unknown",
                    duplicate_reason=duplicate_reason,
                )
            generation_attempt.reset(token)
            provider_timeout.reset(timeout_token)
        detail = ("AI could not generate a valid question" if failure_status == 502
                  else "AI provider unavailable")
        if attempt == policy.max_attempts:
            event("generation_retries_exhausted", attempts=attempt,
                  reason="duplicate_exhausted" if duplicate_rejections and not other_failures
                  else "generation_failed")
            raise failure(failure_status, detail)
        delay = policy.backoff(attempt)
        # Do not retry sooner than the server requests, or sleep beyond our limits.
        if delay_hint is not None:
            if delay_hint > policy.backoff_max_seconds:
                event("generation_retry_suppressed", reason="retry_after_exceeds_limit")
                raise HTTPException(status_code=failure_status, detail=detail)
            delay = max(delay, delay_hint)
        if delay >= deadline - perf_counter():
            event("generation_budget_exhausted")
            raise failure(failure_status, detail)
        event("generation_retry_scheduled", failed_attempt=attempt, delay_ms=delay * 1000)
        sleep(delay)
    return question


@app.get("/api/games/{game_id}/question", response_model=PublicQuestion)
def get_question(game_id: str) -> PublicQuestion:
    session = find_session(game_id)
    lock_start = perf_counter()
    event("session_lock_wait_started")
    with session.lock:
        event("session_lock_acquired", duration_ms=elapsed_ms(lock_start))
        if session.state.phase not in ("main", "bonus"):
            raise HTTPException(status_code=409, detail="Game has ended")
        if session.question is None:
            event("question_cache", hit=False)
            if not session.categories:
                raise HTTPException(status_code=500, detail="No unused categories")
            difficulty = session.state.current_difficulty
            category = session.categories[-1]
            session.question = generate_valid_question(difficulty, session.language, category)
            session.question_id = str(uuid4())
            session.categories.pop()
        else:
            event("question_cache", hit=True)
        question = session.question
        assert question is not None and session.question_id is not None
        return PublicQuestion(question_id=session.question_id,
                              number=session.state.correct_main_answers + 1,
                              difficulty=question.difficulty,
                              question=question.question,
                              answers=question.answers,
                              bonus=session.state.phase == "bonus")


@app.post("/api/games/{game_id}/answer", response_model=AnswerFeedback)
def submit_answer(game_id: str, payload: AnswerInput) -> AnswerFeedback:
    session = find_session(game_id)
    with session.lock:
        if session.question is None or session.question_id != payload.question_id:
            raise HTTPException(status_code=409, detail="No matching unanswered question")
        correct_key = session.question.correct_answer
        outcome = session.state.submit_answer(payload.answer == correct_key)
        # Replays no longer match: question IDs are single-use.
        session.question = None
        session.question_id = None
        return AnswerFeedback(correct=outcome.correct, correct_answer=correct_key,
                              phase=outcome.phase,
                              correct_main_answers=session.state.correct_main_answers,
                              achievement=outcome.achievement)


# Versioned authenticated API. Legacy /api/games stays available until
# Angular is migrated and the in-memory API tests have been retired.
from persistence.game_routes import router as persistent_games_router  # noqa: E402
app.include_router(persistent_games_router)
