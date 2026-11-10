"""Community thumbs feedback and durable vote-triggered review.

Revision ID: 0013_question_votes_pool_priority
Revises: 0012_appeal_qa_free_quota
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_question_votes_pool_priority"
down_revision = "0012_appeal_qa_free_quota"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("questions", sa.Column(
        "vote_score", sa.Integer(), nullable=False, server_default="0",
    ))
    op.create_index(
        "ix_questions_pool_voted", "questions",
        ["status", "language", "topic_id", "requested_difficulty", "vote_score"],
    )
    op.create_table(
        "question_votes",
        sa.Column("question_id", sa.Uuid(),
                  sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("value IN (-1, 1)", name="ck_question_vote_value"),
    )
    op.create_index("ix_question_votes_user", "question_votes", ["user_id"])
    op.create_table(
        "question_vote_reviews",
        sa.Column("question_id", sa.Uuid(),
                  sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("score_at_trigger", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("verdict", sa.Text()),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','retry_pending','upheld','rejected',"
            "'inconclusive','technical_unresolved')",
            name="ck_question_vote_review_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_question_vote_review_attempts"),
    )
    op.create_index(
        "ix_question_vote_review_due", "question_vote_reviews",
        ["status", "next_retry_at"],
    )


def downgrade():
    op.drop_index("ix_question_vote_review_due", table_name="question_vote_reviews")
    op.drop_table("question_vote_reviews")
    op.drop_index("ix_question_votes_user", table_name="question_votes")
    op.drop_table("question_votes")
    op.drop_index("ix_questions_pool_voted", table_name="questions")
    op.drop_column("questions", "vote_score")
