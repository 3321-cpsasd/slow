"""Add append-only study activity pulses.

Revision ID: 0055_study_activity_time
Revises: 0054_m3_pilot_foundations
"""

from alembic import op
import sqlalchemy as sa


revision = "0055_study_activity_time"
down_revision = "0054_m3_pilot_foundations"
branch_labels = None
depends_on = None


def upgrade():
    if "study_activity_pulses" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "study_activity_pulses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("client_session_id", sa.String(length=80), nullable=False),
        sa.Column("client_sequence", sa.Integer(), nullable=False),
        sa.Column("activity_kind", sa.String(length=32), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=True),
        sa.Column("timezone_snapshot", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("measurement_rule_version", sa.String(length=40), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["learning_run_id"], ["learning_runs.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "event_id",
            name="uq_study_activity_pulses_user_event",
        ),
    )
    for column in (
        "user_id",
        "client_session_id",
        "activity_kind",
        "section_id",
        "learning_run_id",
        "received_at",
    ):
        op.create_index(
            op.f(f"ix_study_activity_pulses_{column}"),
            "study_activity_pulses",
            [column],
        )


def downgrade():
    if "study_activity_pulses" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("study_activity_pulses")
