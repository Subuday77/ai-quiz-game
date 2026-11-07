"""Record an optional gold wager for the sixteenth quiz question.

Revision ID: 0007_bonus_wager
Revises: 0006_platinum_helper
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_bonus_wager"
down_revision = "0006_platinum_helper"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "game_runs",
        sa.Column("bonus_wager_accepted", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column("game_runs", "bonus_wager_accepted")
