"""Persist Platinum Helper purchases, calls and star movements.

Revision ID: 0005_platinum_helper
Revises: 0004_bonus_topic_rotation
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_platinum_helper"
down_revision = "0004_bonus_topic_rotation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platinum_helper_purchases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("game_runs.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("price", sa.SmallInteger(), nullable=False),
        sa.Column("profession", sa.String(80), nullable=False),
        sa.Column("profile", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("consultations_used", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("refunded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price IN (1,2)", name="ck_helper_price"),
        sa.CheckConstraint("consultations_used BETWEEN 0 AND 3", name="ck_helper_used"),
    )
    op.create_index("ix_platinum_helper_purchases_user_id", "platinum_helper_purchases", ["user_id"])
    op.create_table(
        "platinum_helper_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("purchase_id", sa.Uuid(), sa.ForeignKey("platinum_helper_purchases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("question_attempt_id", sa.Uuid(), sa.ForeignKey("question_attempts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("topic", sa.String(60), nullable=False),
        sa.Column("outcome", sa.String(15), nullable=False),
        sa.Column("selected_key", sa.String(1)),
        sa.Column("status", sa.String(15), nullable=False, server_default="pending"),
        sa.Column("response_text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("purchase_id", "request_key", name="uq_helper_call_request"),
        sa.CheckConstraint("outcome IN ('KNOW','MISTAKEN','UNSURE')", name="ck_helper_outcome"),
        sa.CheckConstraint("status IN ('pending','completed','failed')", name="ck_helper_status"),
        sa.CheckConstraint("selected_key IS NULL OR selected_key IN ('A','B','C','D')", name="ck_helper_selected_key"),
    )
    op.create_index("ix_platinum_helper_calls_purchase_id", "platinum_helper_calls", ["purchase_id"])
    op.create_table(
        "platinum_star_movements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("purchase_id", sa.Uuid(), sa.ForeignKey("platinum_helper_purchases.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("purchase_id", "reason", name="uq_platinum_purchase_reason"),
        sa.CheckConstraint("amount <> 0", name="ck_platinum_movement_nonzero"),
        sa.CheckConstraint("reason IN ('purchase','refund')", name="ck_platinum_movement_reason"),
    )
    op.create_index("ix_platinum_star_movements_user_id", "platinum_star_movements", ["user_id"])

    op.create_table(
        "gold_star_movements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("game_runs.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("amount", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount = -1 AND reason = 'bonus_lost'", name="ck_gold_bonus_loss"),
    )
    op.create_index("ix_gold_star_movements_user_id", "gold_star_movements", ["user_id"])


def downgrade():
    op.drop_index("ix_gold_star_movements_user_id", "gold_star_movements")
    op.drop_table("gold_star_movements")
    op.drop_index("ix_platinum_star_movements_user_id", "platinum_star_movements")
    op.drop_table("platinum_star_movements")
    op.drop_index("ix_platinum_helper_calls_purchase_id", "platinum_helper_calls")
    op.drop_table("platinum_helper_calls")
    op.drop_index("ix_platinum_helper_purchases_user_id", "platinum_helper_purchases")
    op.drop_table("platinum_helper_purchases")
