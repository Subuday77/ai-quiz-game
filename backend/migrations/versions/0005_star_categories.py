"""Per-run category restrictions and replaceable paid question versions.

Revision ID: 0005_star_categories
Revises: 0004_bonus_topic_rotation
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_star_categories"
down_revision = "0004_bonus_topic_rotation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_runs", sa.Column("allowed_categories", JSONB(), nullable=True))
    op.add_column("question_attempts", sa.Column(
        "revision", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("question_attempts", sa.Column(
        "superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("question_attempts", sa.Column(
        "category_change_count", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("question_attempts", sa.Column(
        "manual_category_used", sa.SmallInteger(), nullable=False, server_default="0"))
    op.drop_constraint("uq_run_question_number", "question_attempts", type_="unique")
    op.create_unique_constraint(
        "uq_run_question_revision", "question_attempts", ["run_id", "number", "revision"])
    op.create_check_constraint("ck_attempt_category_changes", "question_attempts",
                               "revision >= 0 AND category_change_count BETWEEN 0 AND 2")
    op.create_check_constraint("ck_attempt_manual_changes", "question_attempts",
                               "manual_category_used BETWEEN 0 AND 1")
    op.create_index("ix_attempt_pending_supersede", "question_attempts",
                    ["run_id", "answered_at", "superseded_at"])


def downgrade():
    # Multiple versions per slot can exist after silver perks. Downgrading
    # must not silently lose exposed-question history.
    connection = op.get_bind()
    duplicate = connection.execute(sa.text("""
        SELECT 1 FROM question_attempts GROUP BY run_id, number
        HAVING COUNT(*) > 1 LIMIT 1
    """)).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade: question category revisions exist")
    op.drop_index("ix_attempt_pending_supersede", table_name="question_attempts")
    op.drop_constraint("ck_attempt_manual_changes", "question_attempts", type_="check")
    op.drop_constraint("ck_attempt_category_changes", "question_attempts", type_="check")
    op.drop_constraint("uq_run_question_revision", "question_attempts", type_="unique")
    op.create_unique_constraint(
        "uq_run_question_number", "question_attempts", ["run_id", "number"])
    for field in ("manual_category_used", "category_change_count", "superseded_at", "revision"):
        op.drop_column("question_attempts", field)
    op.drop_column("game_runs", "allowed_categories")
