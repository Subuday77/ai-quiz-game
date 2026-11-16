"""Premium membership and webhook receipt tables.

Revision ID: 0017_premium_memberships
Revises: 0016_auth_rate_buckets
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_premium_memberships"
down_revision = "0016_auth_rate_buckets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "premium_memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_subscription_id", sa.String(100), nullable=False),
        sa.Column("provider_supporter_id", sa.String(100), nullable=False),
        sa.Column("plan", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "provider_subscription_id",
                            name="uq_premium_provider_subscription"),
        sa.CheckConstraint("plan IN ('month','year')", name="ck_premium_plan"),
        sa.CheckConstraint("status IN ('active','cancelled','paused')", name="ck_premium_status"),
    )
    op.create_index("ix_premium_memberships_user_id", "premium_memberships", ["user_id"])
    op.create_table(
        "premium_webhook_events",
        sa.Column("provider_event_id", sa.String(100), primary_key=True),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
    )


def downgrade():
    op.drop_table("premium_webhook_events")
    op.drop_index("ix_premium_memberships_user_id", table_name="premium_memberships")
    op.drop_table("premium_memberships")
