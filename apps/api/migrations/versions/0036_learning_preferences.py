"""Add versioned learner presentation preferences.

Revision ID: 0036_learning_preferences
Revises: 0035_user_feedback
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_learning_preferences"
down_revision = "0035_user_feedback"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("user_profiles")
    }
    if "preferences_json" not in columns:
        op.add_column(
            "user_profiles",
            sa.Column(
                "preferences_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade():
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("user_profiles")
    }
    if "preferences_json" in columns:
        op.drop_column("user_profiles", "preferences_json")
