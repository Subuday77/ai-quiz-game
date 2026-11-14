"""Persist per-issuance A/B/C/D shuffle, legacy attempts keep original order.

Revision ID: 0014_attempt_choice_order
Revises: 0013_votes_pool
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_attempt_choice_order"
down_revision = "0013_votes_pool"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("question_attempts", sa.Column("choice_order", sa.String(4), nullable=True))


def downgrade():
    op.drop_column("question_attempts", "choice_order")
