"""Persist classic/easy mode on every round; old rounds remain classic.

Revision ID: 0020_game_mode
Revises: 0019_shared_generation_failure
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_game_mode"
down_revision = "0019_shared_generation_failure"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_runs", sa.Column(
        "mode", sa.String(length=10), nullable=False, server_default="classic"))
    op.create_check_constraint(
        "ck_run_mode", "game_runs", "mode IN ('classic','easy')")


def downgrade():
    op.drop_constraint("ck_run_mode", "game_runs", type_="check")
    op.drop_column("game_runs", "mode")
