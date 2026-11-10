"""Optional per-user UTC-month QA credits for additional free appeal filings.

Revision ID: 0012_appeal_qa_free_quota
Revises: 0011_appeal_question_expenses

Not active unless QUIZ_QA_APPEAL_CREDITS_ENABLED=true is deliberately set.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_appeal_qa_free_quota"
down_revision = "0011_appeal_question_expenses"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "appeal_qa_free_quota",
        sa.Column("user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("month_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("extra_free", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("extra_free >= 0 AND extra_free <= 1000",
                           name="ck_appeal_qa_free_limit"),
    )


def downgrade():
    op.drop_table("appeal_qa_free_quota")
