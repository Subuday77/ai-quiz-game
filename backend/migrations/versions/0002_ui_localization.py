"""Persist independent player UI language and revisioned localized UI strings.

Revision ID: 0002_ui_localization
Revises: 0001_initial
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_ui_localization"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("ui_language", sa.String(length=20),
                           nullable=False, server_default="en")
    )
    op.create_table(
        "ui_translations",
        sa.Column("locale_code", sa.String(length=20), nullable=False),
        sa.Column("string_key", sa.String(length=120), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("locale_code", "string_key"),
    )
    op.create_table(
        "ui_locale_sync",
        sa.Column("locale_code", sa.String(length=20), nullable=False),
        sa.Column("lease_token", sa.String(length=64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("locale_code"),
    )


def downgrade():
    op.drop_table("ui_locale_sync")
    op.drop_table("ui_translations")
    op.drop_column("users", "ui_language")
