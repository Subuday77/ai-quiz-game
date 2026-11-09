"""Persist exact per-attempt perk costs for appeal reimbursement.

Revision ID: 0011_appeal_question_expenses
Revises: 0010_question_appeals
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_appeal_question_expenses"
down_revision = "0010_question_appeals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("question_attempts", sa.Column(
        "appeal_expenses", sa.JSON().with_variant(
            __import__("sqlalchemy.dialects.postgresql", fromlist=["JSONB"]).JSONB(),
            "postgresql"),
        nullable=False, server_default=sa.text("'{}'"),
    ))


def downgrade():
    op.drop_column("question_attempts", "appeal_expenses")
