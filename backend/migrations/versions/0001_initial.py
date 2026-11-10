"""Initial quiz PostgreSQL schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-24

All table/constraint definitions are frozen in snapshots.schema_v1, independent
from runtime persistence.models. Do not edit migration or snapshot after
databases have applied this revision.
"""
from alembic import op
from migrations.snapshots.schema_v1 import Base as SchemaV1

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    SchemaV1.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade():
    SchemaV1.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
