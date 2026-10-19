"""Local-development FastAPI backend for AI Quiz Game.

Sessions are intentionally in memory for the first playable web prototype.
Do not deploy publicly before persistent storage, authentication and quotas.
"""

import json
import random
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter
from uuid import uuid4

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

load_dotenv(find_dotenv())

from consts import LANGUAGE_DICT, QUESTION_CATEGORIES  # noqa: E402
from game_logic import GameState  # noqa: E402
from diagnostics import elapsed_ms, event, generation_attempt, request_id, timed_stage  # noqa: E402
from question_generator import create_question  # noqa: E402
from question_schema import Choice, PublicQuestion, QuizQuestion  # noqa: E402

app = FastAPI(title="AI Quiz Game", version="0.1.0")


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
            # Invalid JSON or schema gets a bounded retry. Provider/auth errors are not retried here.
            for attempt in range(3):
                token = generation_attempt.set(attempt + 1)
                event("generation_attempt_started")
                try:
                    raw = create_question(difficulty, session.language, category)
                    with timed_stage("question_validation"):
                        question = QuizQuestion.model_validate(raw)
                        if question.difficulty != difficulty:
                            raise ValueError("AI returned the wrong difficulty")
                    event("generation_attempt_completed", outcome="success")
                    break
                except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                    event("generation_attempt_completed", outcome="invalid_question",
                          error_type=type(exc).__name__)
                except Exception as exc:
                    event("generation_attempt_completed", outcome="provider_failure",
                          error_type=type(exc).__name__)
                    raise HTTPException(status_code=503, detail="AI provider unavailable") from None
                finally:
                    generation_attempt.reset(token)
            else:
                raise HTTPException(status_code=502, detail="AI could not generate a valid question")
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
