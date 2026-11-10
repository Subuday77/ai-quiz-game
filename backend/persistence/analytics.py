"""Derived player statistics, calculated from immutable runs and answer events.

Until existing game sessions are migrated to PostgreSQL these aggregates will
be zero. Difficulty is numeric metadata, not a semantic topic tag.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from persistence.auth import authenticated_user
from persistence.database import get_db
from persistence.models import (
    AchievementDefinition, GameRun, Question, QuestionAttempt, StarAward,
    Topic, User, UserAchievement, WalletTransaction,
)

router = APIRouter(prefix="/api/profile", tags=["Profile"])


class TopicStats(BaseModel):
    topic: str
    answered: int
    correct: int
    success_percent: float
    share_percent: float
    mean_requested_difficulty: float | None
    mean_answer_seconds: float | None


class StatsOut(BaseModel):
    games_played: int
    total_score: int
    best_round_score: int
    wallet_balance: int
    star_awards: dict[str, int]
    achievements: list[str]
    answered_questions: int
    correct_answers: int
    topics: list[TopicStats]


@router.get("/me/stats", response_model=StatsOut)
def personal_statistics(user: User = Depends(authenticated_user),
                        db: Session = Depends(get_db)):
    totals = db.execute(select(
        func.count(GameRun.id),
        func.coalesce(func.sum(GameRun.round_score), 0),
        func.coalesce(func.max(GameRun.round_score), 0),
    ).where(GameRun.user_id == user.id)).one()
    wallet = db.scalar(select(func.coalesce(func.sum(WalletTransaction.amount), 0))
                       .where(WalletTransaction.user_id == user.id))
    medal_rows = db.execute(select(StarAward.tier, func.count(StarAward.id))
                           .where(StarAward.user_id == user.id)
                           .group_by(StarAward.tier)).all()
    star_awards = {tier: count for tier, count in medal_rows}
    achievements = list(db.scalars(
        select(UserAchievement.achievement_code)
        .where(UserAchievement.user_id == user.id)
        .order_by(UserAchievement.earned_at)
    ))
    correct_flag = case((QuestionAttempt.is_correct.is_(True), 1), else_=0)
    rows = db.execute(
        select(Topic.slug,
               func.count(QuestionAttempt.id),
               func.sum(correct_flag),
               func.avg(QuestionAttempt.requested_difficulty),
               func.avg(QuestionAttempt.answer_duration_ms))
        .select_from(QuestionAttempt)
        .join(GameRun, GameRun.id == QuestionAttempt.run_id)
        .join(Question, Question.id == QuestionAttempt.question_id)
        .join(Topic, Topic.id == Question.topic_id)
        .where(GameRun.user_id == user.id,
               QuestionAttempt.answered_at.is_not(None))
        .group_by(Topic.slug).order_by(Topic.slug)
    ).all()
    total_answers = sum(int(row[1]) for row in rows)
    topics = [
        TopicStats(
            topic=slug,
            answered=int(answered),
            correct=int(correct or 0),
            success_percent=round(100 * int(correct or 0) / int(answered), 2),
            share_percent=round(100 * int(answered) / total_answers, 2),
            mean_requested_difficulty=(round(float(avg_d), 2) if avg_d is not None else None),
            mean_answer_seconds=(round(float(avg_ms) / 1000, 2) if avg_ms is not None else None),
        )
        for slug, answered, correct, avg_d, avg_ms in rows
    ]
    return StatsOut(
        games_played=int(totals[0]),
        total_score=int(totals[1]),
        best_round_score=int(totals[2]),
        wallet_balance=int(wallet or 0),
        star_awards=star_awards,
        achievements=achievements,
        answered_questions=total_answers,
        correct_answers=sum(t.correct for t in topics),
        topics=topics,
    )
