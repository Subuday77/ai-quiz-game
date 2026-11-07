"""Persist question-level Platinum Helper competence, without rewriting history.

Revision ID: 0009_question_competence_area
Revises: 0008_single_active_round

Legacy rows remain NULL. Runtime can resolve high-confidence English canonical
fact/concept slugs without paid calls; otherwise explicitly retains the old
category mapping. New tagged questions save the independently reviewed area.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_question_competence_area"
down_revision = "0008_single_active_round"
branch_labels = None
depends_on = None

_ALLOWED = (
    "Physics", "Mathematics", "Chemistry", "Biology", "Medicine", "History",
    "Geography", "Literature", "Art", "Music", "Cinema", "Mythology",
    "Sports", "Technology", "Astronomy", "Meteorology", "Culture",
    "Engineering", "Agriculture", "Comics",
)
_CHECK = "competence_area IN (" + ", ".join(f"'{area}'" for area in _ALLOWED) + ")"


def upgrade():
    op.add_column(
        "questions", sa.Column("competence_area", sa.String(length=40), nullable=True)
    )
    op.create_check_constraint("ck_question_competence_area", "questions", _CHECK)


def downgrade():
    op.drop_constraint("ck_question_competence_area", "questions", type_="check")
    op.drop_column("questions", "competence_area")
