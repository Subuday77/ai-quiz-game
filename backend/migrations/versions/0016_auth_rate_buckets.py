"""Persist anonymous auth request throttles.

Revision ID: 0016_auth_rate_buckets
Revises: 0015_email_accounts
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_auth_rate_buckets"
down_revision = "0015_email_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_rate_buckets",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("window_id", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
    )
    op.create_index("ix_auth_rate_buckets_window_id",
                    "auth_rate_buckets", ["window_id"])


def downgrade():
    op.drop_index("ix_auth_rate_buckets_window_id", table_name="auth_rate_buckets")
    op.drop_table("auth_rate_buckets")
