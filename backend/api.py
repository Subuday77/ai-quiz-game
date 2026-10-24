"""Local-development FastAPI backend for AI Quiz Game.

Sessions are intentionally in memory for the first playable web prototype.
Do not deploy publicly before persistent storage, authentication and quotas.
"""

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
from quality_errors import QualityConfigurationError, QualityRegenerationError  # noqa: E402
from question_generator import create_question  # noqa: E402
from question_schema import Choice, PublicQuestion, QuizQuestion  # noqa: E402
from persistence.auth import router as auth_router  # noqa: E402
from persistence.analytics import router as profile_router  # noqa: E402

app = FastAPI(title="AI Quiz Game", version="0.1.0")
app.include_router(auth_router)  # Accounts are DB-backed; game sessions still in memory.
app.include_router(profile_router)  # Derived statistics are initially empty until game DB migration.


@app.middleware("http")
async def question_diagnostics(request: Request, call_next):
    parts = request.url.path.split("/")
    if not (request.method == "GET" and len(parts) == 5
            and parts[1:3] == ["api", "games"] and parts[4] == "question"):
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
            try:
                policy = GenerationPolicy.from_env()
                quality_mode = quality_mode_from_env()
                leak_mode = local_leak_mode_from_env()
            except (ValueError, QualityConfigurationError):
                event("generation_configuration_failed")
                raise HTTPException(status_code=503, detail="AI provider unavailable") from None
            deadline = perf_counter() + policy.budget_seconds
            event("generation_policy", budget_ms=policy.budget_seconds * 1000,
                  max_attempts=policy.max_attempts,
                  provider_timeout_ms=policy.provider_timeout_seconds * 1000, sdk_max_retries=0)
            for attempt in range(1, policy.max_attempts + 1):
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    event("generation_budget_exhausted")
                    raise HTTPException(status_code=503, detail="AI provider unavailable")
                token = generation_attempt.set(attempt)
                timeout_token = provider_timeout.set(min(policy.provider_timeout_seconds, remaining))
                event("generation_attempt_started")
                failure_status = 503
                delay_hint = None
                try:
                    raw = create_question(difficulty, session.language, category)
                    with timed_stage("question_validation"):
                        question = QuizQuestion.model_validate(raw)
                        if question.difficulty != difficulty:
                            raise InvalidQuestionError("AI returned the wrong difficulty")
                    check_local_leak(question, leak_mode)
                    check_quality(question, difficulty, session.language, quality_mode,
                                  deadline, policy.provider_timeout_seconds, clock=perf_counter)
                    event("generation_attempt_completed", outcome="success")
                    break
                except QualityRegenerationError:
                    failure_status = 502
                    event("generation_attempt_completed", outcome="quality_rejected")
                except (json.JSONDecodeError, ValidationError, InvalidQuestionError) as exc:
                    failure_status = 502
                    event("generation_attempt_completed", outcome="invalid_question",
                          error_type=type(exc).__name__)
                except Exception as exc:
                    retryable = is_transient(exc)
                    event("generation_attempt_completed", outcome="provider_failure",
                          error_type=type(exc).__name__, retryable=retryable)
                    if not retryable:
                        raise HTTPException(status_code=503, detail="AI provider unavailable") from None
                    delay_hint = retry_after_seconds(exc)
                finally:
                    generation_attempt.reset(token)
                    provider_timeout.reset(timeout_token)
                detail = ("AI could not generate a valid question" if failure_status == 502
                          else "AI provider unavailable")
                if attempt == policy.max_attempts:
                    event("generation_retries_exhausted", attempts=attempt)
                    raise HTTPException(status_code=failure_status, detail=detail)
                delay = policy.backoff(attempt)
                # Do not retry sooner than the server requests, or sleep beyond our limits.
                if delay_hint is not None:
                    if delay_hint > policy.backoff_max_seconds:
                        event("generation_retry_suppressed", reason="retry_after_exceeds_limit")
                        raise HTTPException(status_code=failure_status, detail=detail)
                    delay = max(delay, delay_hint)
                if delay >= deadline - perf_counter():
                    event("generation_budget_exhausted")
                    raise HTTPException(status_code=failure_status, detail=detail)
                event("generation_retry_scheduled", failed_attempt=attempt, delay_ms=delay * 1000)
                sleep(delay)
            session.question = question
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
