"""Add formal transfer-task facts to the application task chain.

Revision ID: 0071_formal_transfer_tasks
Revises: 0070_cross_series_identity_publication
"""

from alembic import op
import sqlalchemy as sa


revision = "0071_formal_transfer_tasks"
down_revision = "0070_cross_series_identity_publication"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {
        item["name"]
        for item in inspector.get_columns("capability_application_task_versions")
    }
    additions = (
        ("task_kind", sa.String(length=32), "standard_application"),
        ("unfamiliarity_basis_json", sa.Text(), "{}"),
        ("recombination_requirements_json", sa.Text(), "[]"),
        ("context_fingerprint", sa.String(length=64), ""),
    )
    for name, kind, default in additions:
        if name not in columns:
            op.add_column(
                "capability_application_task_versions",
                sa.Column(name, kind, nullable=False, server_default=default),
            )
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(
            "capability_application_task_versions"
        )
    }
    for name, column in (
        ("ix_cap_app_task_task_kind", "task_kind"),
        ("ix_cap_app_task_context_fingerprint", "context_fingerprint"),
    ):
        if name not in indexes:
            op.create_index(name, "capability_application_task_versions", [column])


def downgrade():
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(
            "capability_application_task_versions"
        )
    }
    for name in (
        "ix_cap_app_task_context_fingerprint",
        "ix_cap_app_task_task_kind",
    ):
        if name in indexes:
            op.drop_index(name, table_name="capability_application_task_versions")
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(
            "capability_application_task_versions"
        )
    }
    for name in (
        "context_fingerprint",
        "recombination_requirements_json",
        "unfamiliarity_basis_json",
        "task_kind",
    ):
        if name in columns:
            op.drop_column("capability_application_task_versions", name)
