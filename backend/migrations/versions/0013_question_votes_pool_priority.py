"""Community thumbs feedback and durable vote-triggered review.

Revision ID: 0013_votes_pool
Revises: 0012_appeal_qa_free_quota
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_votes_pool"
down_revision = "0012_appeal_qa_free_quota"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("questions", sa.Column(
        "vote_score", sa.Integer(), nullable=False, server_default="0",
    ))
    op.add_column("questions", sa.Column(
        "working_vote_score", sa.Integer(), nullable=False, server_default="0",
    ))
    op.add_column("questions", sa.Column(
        "community_referrals", sa.SmallInteger(), nullable=False, server_default="0",
    ))
    op.create_check_constraint(
        "ck_community_referrals", "questions",
        "community_referrals BETWEEN 0 AND 3",
    )
    op.create_index(
        "ix_questions_pool_voted", "questions",
        ["status", "language", "topic_id", "requested_difficulty", "working_vote_score"],
    )
    op.create_table(
        "question_votes",
        sa.Column("question_id", sa.Uuid(),
                  sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("run_id", sa.Uuid(),
                  sa.ForeignKey("game_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("value IN (-1, 1)", name="ck_question_vote_value"),
    )
    op.create_index("ix_question_votes_user", "question_votes", ["user_id"])
    op.create_table(
        "question_vote_reviews",
        sa.Column("question_id", sa.Uuid(),
                  sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("referral_number", sa.SmallInteger(), primary_key=True),
        sa.Column("trigger_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("score_at_trigger", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("verdict", sa.Text()),
        sa.Column("judge_model", sa.String(130)),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','retry_pending','upheld','rejected',"
            "'inconclusive','technical_unresolved','auto_banned')",
            name="ck_question_vote_review_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_question_vote_review_attempts"),
        sa.CheckConstraint("referral_number BETWEEN 1 AND 3",
                           name="ck_vote_referral_number"),
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
    op.drop_constraint("ck_community_referrals", "questions", type_="check")
    op.drop_column("questions", "community_referrals")
    op.drop_column("questions", "working_vote_score")
    op.drop_column("questions", "vote_score")
