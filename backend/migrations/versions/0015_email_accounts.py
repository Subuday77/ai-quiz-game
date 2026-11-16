"""Add verified email identity and one-time account tokens (expand-only).

Revision ID: 0015_email_accounts
Revises: 0014_attempt_choice_order
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_email_accounts"
down_revision = "0014_attempt_choice_order"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("uq_users_email", "users", ["email"], unique=True)
    op.create_table(
        "account_email_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose IN ('verify_email', 'reset_password')", name="ck_account_email_token_purpose"),
    )
    op.create_index("ix_account_email_tokens_user", "account_email_tokens", ["user_id"])


def downgrade():
    op.drop_index("ix_account_email_tokens_user", table_name="account_email_tokens")
    op.drop_table("account_email_tokens")
    op.drop_index("uq_users_email", table_name="users")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")
