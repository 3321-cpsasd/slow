"""artifact attachment object metadata

Revision ID: 0003_artifact_attachments
Revises: 0002_learning_loop_gates
"""

from alembic import op

from app.infrastructure.tables import ArtifactAttachment

revision = "0003_artifact_attachments"
down_revision = "0002_learning_loop_gates"
branch_labels = None
depends_on = None


def upgrade():
    ArtifactAttachment.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_table("artifact_attachments")
