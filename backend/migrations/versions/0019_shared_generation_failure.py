"""Persist a sanitized question-generation failure for cross-device retry.

Revision ID: 0019_shared_generation_failure
Revises: 0018_question_generation

This is a new migration because 0018 is already applied on the VPS.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_shared_generation_failure"
down_revision = "0018_question_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_runs", sa.Column(
        "question_generation_error_code", sa.String(32), nullable=True))


def downgrade():
    op.drop_column("game_runs", "question_generation_error_code")
