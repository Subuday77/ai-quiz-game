"""Persist player appeals and per-question moderation state.

Revision ID: 0010_question_appeals
Revises: 0009_question_competence_area
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_question_appeals"
down_revision = "0009_question_competence_area"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "question_appeals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("question_id", sa.Uuid(),
                  sa.ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(),
                  sa.ForeignKey("question_attempts.id", ondelete="SET NULL")),
        sa.Column("run_id", sa.Uuid(),
                  sa.ForeignKey("game_runs.id", ondelete="SET NULL")),
        sa.Column("user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("idempotency_key", sa.String(110), nullable=False, unique=True),
        sa.Column("question_snapshot", sa.JSON(), nullable=False),
        sa.Column("language", sa.String(25), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_links", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("before_answer", sa.Boolean(), nullable=False),
        sa.Column("question_number", sa.SmallInteger(), nullable=False),
        sa.Column("deposit_tier", sa.String(15), nullable=False),
        sa.Column("filing_fee_bronze", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(25), nullable=False, server_default="pending"),
        sa.Column("verdict_en", sa.Text()),
        sa.Column("verdict_localized", sa.Text()),
        sa.Column("verdict_data", sa.JSON()),
        sa.Column("judge_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("deposit_refunded_at", sa.DateTime(timezone=True)),
        sa.Column("compensated_at", sa.DateTime(timezone=True)),
        sa.Column("bonus_wager_refunded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("question_number BETWEEN 1 AND 16", name="ck_appeal_number"),
        sa.CheckConstraint("filing_fee_bronze >= 0", name="ck_appeal_fee"),
        sa.CheckConstraint("deposit_tier IN ('bronze','silver','gold')",
                           name="ck_appeal_deposit"),
        sa.CheckConstraint(
            "status IN ('pending','retry_pending','upheld','rejected','technical_unresolved')",
            name="ck_appeal_status"),
        sa.UniqueConstraint("user_id", "question_id", name="uq_appeal_user_question"),
    )
    op.create_index("ix_appeals_question", "question_appeals", ["question_id"])
    op.create_index("ix_appeals_user_submitted", "question_appeals",
                    ["user_id", "submitted_at"])
    op.create_index("ix_appeals_pending", "question_appeals",
                    ["status", "next_retry_at"])
    op.create_table(
        "question_moderation",
        sa.Column("question_id", sa.Uuid(),
                  sa.ForeignKey("questions.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("distinct_complainants", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("upheld_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("permanently_blocked", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("block_reason", sa.String(40)),
        sa.CheckConstraint(
            "distinct_complainants >= 0 AND upheld_count >= 0 AND rejected_count >= 0",
            name="ck_moderation_counters"),
    )


def downgrade():
    op.drop_table("question_moderation")
    op.drop_index("ix_appeals_pending", table_name="question_appeals")
    op.drop_index("ix_appeals_user_submitted", table_name="question_appeals")
    op.drop_index("ix_appeals_question", table_name="question_appeals")
    op.drop_table("question_appeals")
