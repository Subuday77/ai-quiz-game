"""SQLAlchemy 2 declarative schema for accounts, quiz, statistics, quality and economy.

Requested difficulty is *metadata*, NEVER a topic/tag identity.
The ledger and question attempts are the source of truth; user statistics
are derived from them (not duplicated mutable counters).
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    LargeBinary, SmallInteger, String, Text, UniqueConstraint, Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonData = JSON().with_variant(JSONB, "postgresql")


def now_utc():
    return datetime.now(timezone.utc)


def id_column():
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)


def datetime_column():
    return mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = id_column()
    username: Mapped[str] = mapped_column(String(30), nullable=False)
    username_key: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(80))
    preferred_language: Mapped[str] = mapped_column(String(25), nullable=False, default="English")
    theme_key: Mapped[str] = mapped_column(String(50), nullable=False, default="default")
    created_at = datetime_column()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_subject", name="uq_identity_provider_subject"),
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
        CheckConstraint(
            "(provider = 'password' AND password_hash IS NOT NULL) OR "
            "(provider <> 'password' AND password_hash IS NULL)",
            name="ck_identity_password_only",
        ),
    )

    id = id_column()
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at = datetime_column()


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id = id_column()
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at = datetime_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Topic(Base):
    __tablename__ = "topics"
    id = id_column()
    slug: Mapped[str] = mapped_column(String(110), nullable=False, unique=True)
    labels: Mapped[dict] = mapped_column(JsonData, nullable=False, default=dict)


class Tag(Base):
    __tablename__ = "tags"
    id = id_column()
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="concept")
    labels: Mapped[dict] = mapped_column(JsonData, nullable=False, default=dict)
    created_at = datetime_column()
    __table_args__ = (CheckConstraint("kind IN ('topic','concept','entity','keyword')", name="ck_tag_kind"),)


class Question(Base):
    __tablename__ = "questions"
    id = id_column()
    topic_id = mapped_column(Uuid(as_uuid=True), ForeignKey("topics.id"), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(25), nullable=False)
    requested_difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[dict] = mapped_column(JsonData, nullable=False)
    correct_key: Mapped[str] = mapped_column(String(1), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    generator_provider: Mapped[str] = mapped_column(String(40), nullable=False)
    generator_model: Mapped[str] = mapped_column(String(130), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    generation_request_id: Mapped[str | None] = mapped_column(String(100))
    created_at = datetime_column()
    __table_args__ = (
        CheckConstraint("requested_difficulty BETWEEN 0 AND 15", name="ck_question_difficulty"),
        CheckConstraint("correct_key IN ('A','B','C','D')", name="ck_question_correct_key"),
        CheckConstraint("status IN ('pending','approved','rejected','quarantined')", name="ck_question_status"),
        UniqueConstraint("language", "fingerprint", name="uq_question_language_fingerprint"),
        Index("ix_questions_topic_difficulty", "topic_id", "requested_difficulty"),
        Index("ix_questions_approved", "status", "language"),
    )


class QuestionTag(Base):
    __tablename__ = "question_tags"
    __table_args__ = (CheckConstraint("weight BETWEEN 1 AND 100", name="ck_question_tag_weight"),)

    question_id = mapped_column(Uuid(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True)
    tag_id = mapped_column(Uuid(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    origin: Mapped[str] = mapped_column(String(40), nullable=False, default="generator")


class GameRun(Base):
    __tablename__ = "game_runs"
    id = id_column()
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(25), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="main")
    correct_main_answers: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    round_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hints_used: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    bonus_correct: Mapped[bool | None] = mapped_column(Boolean)
    started_at = datetime_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_duration_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("status IN ('main','bonus','completed','lost','abandoned')", name="ck_run_status"),
        CheckConstraint("correct_main_answers BETWEEN 0 AND 15", name="ck_run_progress"),
        CheckConstraint("round_score >= 0 AND hints_used >= 0", name="ck_run_score_hints"),
        CheckConstraint("active_duration_ms IS NULL OR active_duration_ms >= 0", name="ck_run_active_time"),
        Index("ix_runs_leaderboard", "round_score", "finished_at"),
    )


class QuestionAttempt(Base):
    __tablename__ = "question_attempts"
    id = id_column()
    run_id = mapped_column(Uuid(as_uuid=True), ForeignKey("game_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id = mapped_column(Uuid(as_uuid=True), ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    requested_difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    shown_at = datetime_column()
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_key: Mapped[str | None] = mapped_column(String(1))
    is_correct: Mapped[bool | None] = mapped_column(Boolean)
    hints_used: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    answer_duration_ms: Mapped[int | None] = mapped_column(Integer)
    client_active_duration_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("run_id", "number", name="uq_run_question_number"),
        CheckConstraint("number BETWEEN 1 AND 16", name="ck_attempt_number"),
        CheckConstraint("requested_difficulty BETWEEN 0 AND 15", name="ck_attempt_difficulty"),
        CheckConstraint("selected_key IS NULL OR selected_key IN ('A','B','C','D')", name="ck_attempt_selection"),
        CheckConstraint("hints_used >= 0", name="ck_attempt_hints"),
        CheckConstraint("answer_duration_ms IS NULL OR answer_duration_ms >= 0", name="ck_attempt_time"),
        CheckConstraint("client_active_duration_ms IS NULL OR client_active_duration_ms >= 0", name="ck_attempt_client_time"),
    )


class AchievementDefinition(Base):
    __tablename__ = "achievement_definitions"
    code: Mapped[str] = mapped_column(String(70), primary_key=True)
    labels: Mapped[dict] = mapped_column(JsonData, nullable=False, default=dict)
    rule: Mapped[dict] = mapped_column(JsonData, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    id = id_column()
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    achievement_code = mapped_column(String(70), ForeignKey("achievement_definitions.code"), nullable=False)
    run_id = mapped_column(Uuid(as_uuid=True), ForeignKey("game_runs.id"), nullable=True)
    earned_at = datetime_column()
    __table_args__ = (UniqueConstraint("user_id", "achievement_code", name="uq_user_achievement"),)


class StarAward(Base):
    __tablename__ = "star_awards"
    id = id_column()
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    run_id = mapped_column(Uuid(as_uuid=True), ForeignKey("game_runs.id", ondelete="RESTRICT"), nullable=False)
    tier: Mapped[str] = mapped_column(String(15), nullable=False)
    awarded_at = datetime_column()
    __table_args__ = (
        CheckConstraint("tier IN ('bronze','silver','gold','platinum')", name="ck_star_tier"),
        UniqueConstraint("run_id", "tier", name="uq_star_run_tier"),
    )


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id = id_column()
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    run_id = mapped_column(Uuid(as_uuid=True), ForeignKey("game_runs.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(110), nullable=False, unique=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at = datetime_column()
    __table_args__ = (CheckConstraint("amount <> 0", name="ck_wallet_nonzero"),)


class QuestionReview(Base):
    __tablename__ = "question_reviews"
    id = id_column()
    question_id = mapped_column(Uuid(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True)
    checker_type: Mapped[str] = mapped_column(String(60), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(130))
    contract_version: Mapped[str] = mapped_column(String(60), nullable=False)
    decision: Mapped[str] = mapped_column(String(45), nullable=False)
    issues: Mapped[list] = mapped_column(JsonData, nullable=False, default=list)
    details: Mapped[dict] = mapped_column(JsonData, nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at = datetime_column()


class QuestionReport(Base):
    __tablename__ = "question_reports"
    id = id_column()
    question_id = mapped_column(Uuid(as_uuid=True), ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True)
    attempt_id = mapped_column(Uuid(as_uuid=True), ForeignKey("question_attempts.id"), nullable=True)
    user_id = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(35), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    adjudication: Mapped[str | None] = mapped_column(Text)
    adjudicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at = datetime_column()
    __table_args__ = (
        CheckConstraint("status IN ('pending','upheld','rejected','uncertain')", name="ck_report_status"),
        CheckConstraint("reason IN ('incorrect_key','multiple_correct','answer_leak','ambiguous',"
                        "'bad_translation','difficulty_mismatch','other')", name="ck_report_reason"),
    )
