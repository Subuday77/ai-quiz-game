"""Star Shop payment and signed webhook receipt audit.

Revision ID: 0018_star_shop
Revises: 0017_premium_memberships

No historical wallet or About gift is changed by this migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_star_shop"
down_revision = "0017_premium_memberships"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "star_shop_purchases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider_purchase_id", sa.String(100), nullable=False, unique=True),
        sa.Column("user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider_extra_id", sa.String(100), nullable=True),
        sa.Column("bronze_amount", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('credited','unmatched','needs_review','refunded',"
            "'refund_pending','refunded_before_credit')",
            name="ck_star_shop_status",
        ),
        sa.CheckConstraint(
            "bronze_amount >= 0 AND price_cents >= 0", name="ck_star_shop_values",
        ),
    )
    op.create_index("ix_star_shop_purchases_user_id", "star_shop_purchases", ["user_id"])
    op.create_table(
        "star_shop_webhook_events",
        sa.Column("provider_event_id", sa.String(100), primary_key=True),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
    )


def downgrade():
    op.drop_table("star_shop_webhook_events")
    op.drop_index("ix_star_shop_purchases_user_id", table_name="star_shop_purchases")
    op.drop_table("star_shop_purchases")
