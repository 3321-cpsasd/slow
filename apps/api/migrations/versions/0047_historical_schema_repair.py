"""Repair schemas created from known intermediate development snapshots.

Revision ID: 0047_historical_schema_repair
Revises: 0046_daily_mode

This is deliberately a forward repair. Revisions 0028 and 0035 already define
the authoritative shape for fresh databases, but development databases could
have had future ORM tables created before those revisions ran. Their
table-exists guards then preserved the intermediate shape while Alembic moved
on. Do not move these repairs back into the historical revisions: databases
that have already stamped them would not execute the changed code.
"""

from hashlib import sha256

from alembic import op
import sqlalchemy as sa


revision = "0047_historical_schema_repair"
down_revision = "0046_daily_mode"
branch_labels = None
depends_on = None


PROJECTION_TABLES = (
    "assessment_gate_states",
    "knowledge_state_projections",
    "review_states",
)
EVIDENCE_UNIQUE = (
    "observation_id",
    "projection_family",
    "rule_version",
)
FEEDBACK_UNIQUE = ("user_id", "idempotency_key")
REQUIRED_TABLES = {
    *PROJECTION_TABLES,
    "evidence_qualification_events",
    "user_feedback",
}


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(table_name):
    return {
        column["name"]: column
        for column in _inspector().get_columns(table_name)
    }


def _unique_signatures(table_name):
    inspector = _inspector()
    signatures = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(table_name)
    }
    signatures.update(
        tuple(item["column_names"])
        for item in inspector.get_indexes(table_name)
        if item.get("unique") and item.get("column_names")
    )
    return signatures


def _assert_no_duplicates(table_name, column_names):
    table = sa.table(
        table_name,
        *(sa.column(column_name) for column_name in column_names),
    )
    columns = [table.c[column_name] for column_name in column_names]
    duplicate = op.get_bind().execute(
        sa.select(*columns, sa.func.count().label("row_count"))
        .select_from(table)
        .where(sa.and_(*(column.is_not(None) for column in columns)))
        .group_by(*columns)
        .having(sa.func.count() > 1)
        .limit(1)
    ).first()
    if duplicate:
        raise RuntimeError(
            "historical schema repair refused duplicate immutable facts in "
            f"{table_name} for ({', '.join(column_names)})"
        )


def _create_unique_constraint(table_name, constraint_name, column_names):
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name, recreate="always") as batch:
            batch.create_unique_constraint(constraint_name, list(column_names))
        return
    op.create_unique_constraint(constraint_name, table_name, list(column_names))


def _backfill_feedback_metadata():
    feedback = sa.table(
        "user_feedback",
        sa.column("id"),
        sa.column("idempotency_key"),
        sa.column("request_hash"),
    )
    rows = op.get_bind().execute(
        sa.select(
            feedback.c.id,
            feedback.c.idempotency_key,
            feedback.c.request_hash,
        )
    ).mappings()
    for row in rows:
        identifier = str(row["id"])
        values = {}
        if not row["idempotency_key"]:
            digest = sha256(identifier.encode("utf-8")).hexdigest()
            values["idempotency_key"] = f"historical:{digest}"
        if not row["request_hash"]:
            values["request_hash"] = sha256(
                f"historical-feedback:{identifier}".encode("utf-8")
            ).hexdigest()
        if values:
            op.get_bind().execute(
                feedback.update()
                .where(feedback.c.id == row["id"])
                .values(**values)
            )


def _finalize_feedback_schema(create_unique):
    columns = _columns("user_feedback")
    make_idempotency_required = columns["idempotency_key"]["nullable"]
    make_hash_required = columns["request_hash"]["nullable"]
    if not (make_idempotency_required or make_hash_required or create_unique):
        return

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("user_feedback", recreate="always") as batch:
            if make_idempotency_required:
                batch.alter_column(
                    "idempotency_key",
                    existing_type=sa.String(128),
                    nullable=False,
                )
            if make_hash_required:
                batch.alter_column(
                    "request_hash",
                    existing_type=sa.String(64),
                    nullable=False,
                )
            if create_unique:
                batch.create_unique_constraint(
                    "uq_user_feedback_user_idempotency",
                    list(FEEDBACK_UNIQUE),
                )
        return

    if make_idempotency_required:
        op.alter_column(
            "user_feedback",
            "idempotency_key",
            existing_type=sa.String(128),
            nullable=False,
        )
    if make_hash_required:
        op.alter_column(
            "user_feedback",
            "request_hash",
            existing_type=sa.String(64),
            nullable=False,
        )
    if create_unique:
        op.create_unique_constraint(
            "uq_user_feedback_user_idempotency",
            "user_feedback",
            list(FEEDBACK_UNIQUE),
        )


def upgrade():
    existing_tables = set(_inspector().get_table_names())
    missing_tables = REQUIRED_TABLES - existing_tables
    if missing_tables:
        raise RuntimeError(
            "historical schema repair requires canonical predecessor tables: "
            + ", ".join(sorted(missing_tables))
        )

    evidence_needs_unique = (
        EVIDENCE_UNIQUE
        not in _unique_signatures("evidence_qualification_events")
    )
    if evidence_needs_unique:
        _assert_no_duplicates(
            "evidence_qualification_events",
            EVIDENCE_UNIQUE,
        )

    feedback_columns = _columns("user_feedback")
    feedback_had_idempotency = "idempotency_key" in feedback_columns
    feedback_needs_unique = FEEDBACK_UNIQUE not in _unique_signatures(
        "user_feedback"
    )
    if feedback_had_idempotency and feedback_needs_unique:
        _assert_no_duplicates("user_feedback", FEEDBACK_UNIQUE)

    for table_name in PROJECTION_TABLES:
        if "projection_version" not in _columns(table_name):
            with op.batch_alter_table(table_name) as batch:
                batch.add_column(
                    sa.Column(
                        "projection_version",
                        sa.Integer(),
                        nullable=False,
                        server_default="1",
                    )
                )

    if evidence_needs_unique:
        _create_unique_constraint(
            "evidence_qualification_events",
            "uq_evidence_qualification_observation_family_rule",
            EVIDENCE_UNIQUE,
        )

    feedback_columns = _columns("user_feedback")
    missing_feedback_columns = {
        "idempotency_key",
        "request_hash",
    } - set(feedback_columns)
    if missing_feedback_columns:
        with op.batch_alter_table("user_feedback") as batch:
            if "idempotency_key" in missing_feedback_columns:
                batch.add_column(
                    sa.Column("idempotency_key", sa.String(128), nullable=True)
                )
            if "request_hash" in missing_feedback_columns:
                batch.add_column(
                    sa.Column("request_hash", sa.String(64), nullable=True)
                )

    _backfill_feedback_metadata()
    if feedback_needs_unique:
        _assert_no_duplicates("user_feedback", FEEDBACK_UNIQUE)
    _finalize_feedback_schema(feedback_needs_unique)


def downgrade():
    # 0047 introduces no new canonical schema. It only restores invariants
    # already required by 0028 and 0035, so removing them would corrupt 0046.
    pass
