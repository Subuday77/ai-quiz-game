"""Canonical cross-language fact signatures for user-specific duplicate checks.

Revision ID: 0003_question_facts
Revises: 0002_ui_localization
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_question_facts"
down_revision = "0002_ui_localization"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "question_facts",
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("fact_key", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=160), nullable=False),
        sa.Column("relation", sa.String(length=160), nullable=False),
        sa.Column("object", sa.String(length=160), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("question_id"),
    )
    op.create_index("ix_question_facts_fact_key", "question_facts", ["fact_key"])


def downgrade():
    op.drop_index("ix_question_facts_fact_key", table_name="question_facts")
    op.drop_table("question_facts")
