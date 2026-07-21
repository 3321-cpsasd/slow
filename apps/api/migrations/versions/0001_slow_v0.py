"""slow v0 clean schema

Revision ID: 0001_slow_v0
Revises:
"""
from alembic import op
from app.infrastructure.tables import Base

revision = "0001_slow_v0"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(op.get_bind())


def downgrade():
    Base.metadata.drop_all(op.get_bind())
