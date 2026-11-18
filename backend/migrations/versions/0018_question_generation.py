"""Persist a bounded, recoverable per-game question-generation lease.

Revision ID: 0018_question_generation
Revises: 0017_premium_memberships
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_question_generation"
down_revision = "0017_premium_memberships"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_runs", sa.Column(
        "question_generation_token", sa.String(64), nullable=True))
    op.add_column("game_runs", sa.Column(
        "question_generation_until", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("game_runs", "question_generation_until")
    op.drop_column("game_runs", "question_generation_token")
