"""Persist optional bonus topic rotation and generation-failure recovery.

Revision ID: 0004_bonus_topic_rotation
Revises: 0003_question_facts
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_bonus_topic_rotation"
down_revision = "0003_question_facts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "game_runs",
        sa.Column("bonus_topic_index", sa.SmallInteger(), nullable=False,
                  server_default="0"),
    )
    op.add_column(
        "game_runs",
        sa.Column("bonus_topic_change_unlocked", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column("game_runs", "bonus_topic_change_unlocked")
    op.drop_column("game_runs", "bonus_topic_index")
