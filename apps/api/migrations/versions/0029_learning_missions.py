"""Add immutable learning missions and backfill M1 series in place.

Revision ID: 0029_learning_missions
Revises: 0028_trustworthy_assessment_notes
"""

from datetime import datetime, timezone
import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "0029_learning_missions"
down_revision = "0028_trustworthy_assessment_notes"
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name):
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _stable_id(prefix, *parts):
    material = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _criteria_for_series(bind, series_id):
    snapshot = bind.execute(
        sa.text(
            """
            SELECT revision.snapshot_json
            FROM milestone_path_revisions AS revision
            JOIN milestone_paths AS path ON path.id = revision.path_id
            WHERE path.series_id = :series_id
            ORDER BY revision.version ASC
            LIMIT 1
            """
        ),
        {"series_id": series_id},
    ).scalar_one_or_none()
    payload = _load(snapshot, {})
    definition = payload.get("definition", payload)
    criteria = []
    for milestone in definition.get("milestones", []):
        for item in milestone.get("criteria", []):
            statement = str(item.get("statement", "")).strip()
            if statement:
                criteria.append({
                    "key": str(item.get("key") or f"criterion_{len(criteria) + 1}"),
                    "statement": statement,
                    "acceptance": {
                        "evidenceRule": item.get(
                            "evidenceRule", "all_section_quizzes_passed"
                        ),
                        "bookId": item.get("bookId"),
                        "chapterId": item.get("chapterId"),
                    },
                    "source": "m1_milestone_revision",
                })
    if criteria:
        return criteria

    rows = bind.execute(
        sa.text(
            """
            SELECT chapter.id, chapter.objective, book.id AS book_id
            FROM chapters AS chapter
            JOIN books AS book ON book.id = chapter.book_id
            WHERE book.series_id = :series_id
            ORDER BY book.position, chapter.position
            """
        ),
        {"series_id": series_id},
    ).mappings()
    return [
        {
            "key": f"chapter:{row.id}",
            "statement": row.objective,
            "acceptance": {
                "evidenceRule": "all_section_quizzes_passed",
                "bookId": row.book_id,
                "chapterId": row.id,
            },
            "source": "m1_chapter_objective",
        }
        for row in rows
        if row.objective and row.objective.strip()
    ]


def _backfill_missions():
    bind = op.get_bind()
    created_at = datetime.now(timezone.utc)
    series_rows = bind.execute(
        sa.text(
            """
            SELECT series.id AS series_id, series.plan_id, shelf.user_id,
                   plan.topic, plan.role, plan.experience, plan.purpose,
                   plan.depth, plan.details, plan.assumptions_json,
                   plan.created_at
            FROM series
            JOIN learning_plans AS plan ON plan.id = series.plan_id
            JOIN shelves AS shelf ON shelf.id = series.shelf_id
            ORDER BY series.id
            """
        )
    ).mappings()
    for row in series_rows:
        mission_id = _stable_id("mission_version_m1", row.plan_id, 1)
        why = (row.purpose or "").strip()
        inferred_fields = []
        if not why:
            why = "建立系统认知与基础实践准备"
            inferred_fields.append("why")
        capabilities = [
            dict(item)
            for item in bind.execute(
                sa.text(
                    """
                    SELECT position AS bookPosition, title, topic,
                           description AS outcome
                    FROM books
                    WHERE series_id = :series_id
                    ORDER BY position
                    """
                ),
                {"series_id": row.series_id},
            ).mappings()
        ]
        assumptions = _load(row.assumptions_json, [])
        payload = {
            "why": why,
            "targetCapabilities": capabilities,
            "constraints": {"depth": row.depth, "details": row.details or ""},
            "outOfScope": [],
            "assumptions": assumptions,
            "learnerContext": {
                "role": row.role,
                "experience": row.experience,
            },
            "inferredFields": inferred_fields,
            "schemaVersion": "mission_v1",
        }
        payload_hash = hashlib.sha256(_dump(payload).encode()).hexdigest()
        exists = bind.execute(
            sa.text(
                "SELECT id FROM learning_mission_versions WHERE plan_id = :plan_id"
            ),
            {"plan_id": row.plan_id},
        ).scalar_one_or_none()
        if not exists:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO learning_mission_versions (
                        id, plan_id, user_id, version, status, why,
                        target_capabilities_json, constraints_json,
                        out_of_scope_json, assumptions_json, learner_context_json,
                        inferred_fields_json, provenance_json, schema_version,
                        payload_hash, supersedes_id, confirmed_at, created_at
                    ) VALUES (
                        :id, :plan_id, :user_id, 1, 'grandfathered_m1', :why,
                        :capabilities, :constraints, '[]', :assumptions,
                        :learner_context, :inferred_fields, :provenance,
                        'mission_v1', :payload_hash, NULL, NULL, :created_at
                    )
                    """
                ),
                {
                    "id": mission_id,
                    "plan_id": row.plan_id,
                    "user_id": row.user_id,
                    "why": why,
                    "capabilities": _dump(capabilities),
                    "constraints": _dump(payload["constraints"]),
                    "assumptions": _dump(assumptions),
                    "learner_context": _dump(payload["learnerContext"]),
                    "inferred_fields": _dump(inferred_fields),
                    "provenance": _dump({
                        "mode": "m1_migration",
                        "sourcePlanId": row.plan_id,
                        "purposeSource": (
                            "m1_plan" if not inferred_fields else "system_default"
                        ),
                    }),
                    "payload_hash": payload_hash,
                    "created_at": row.created_at or created_at,
                },
            )
        else:
            mission_id = exists

        for position, criterion in enumerate(
            _criteria_for_series(bind, row.series_id), 1
        ):
            stable_key = criterion["key"][:160]
            criterion_id = _stable_id(
                "mission_criterion_m1", row.plan_id, stable_key
            )
            bind.execute(
                sa.text(
                    """
                    INSERT OR IGNORE INTO mission_success_criteria (
                        id, plan_id, stable_key, created_at, retired_at
                    ) VALUES (:id, :plan_id, :stable_key, :created_at, NULL)
                    """
                ),
                {
                    "id": criterion_id,
                    "plan_id": row.plan_id,
                    "stable_key": stable_key,
                    "created_at": row.created_at or created_at,
                },
            )
            bind.execute(
                sa.text(
                    """
                    INSERT OR IGNORE INTO mission_success_criterion_versions (
                        id, mission_version_id, success_criterion_id, position,
                        statement, acceptance_json, provenance_json, created_at
                    ) VALUES (
                        :id, :mission_id, :criterion_id, :position, :statement,
                        :acceptance, :provenance, :created_at
                    )
                    """
                ),
                {
                    "id": _stable_id(
                        "mission_criterion_version_m1", mission_id, criterion_id
                    ),
                    "mission_id": mission_id,
                    "criterion_id": criterion_id,
                    "position": position,
                    "statement": criterion["statement"],
                    "acceptance": _dump(criterion["acceptance"]),
                    "provenance": _dump({"mode": criterion["source"]}),
                    "created_at": row.created_at or created_at,
                },
            )

        bind.execute(
            sa.text(
                """
                UPDATE series
                SET initial_mission_version_id = :mission_id
                WHERE id = :series_id AND initial_mission_version_id IS NULL
                """
            ),
            {"mission_id": mission_id, "series_id": row.series_id},
        )
        runs = bind.execute(
            sa.text(
                "SELECT id, user_id, created_at FROM learning_runs "
                "WHERE series_id = :series_id ORDER BY id"
            ),
            {"series_id": row.series_id},
        ).mappings()
        for run in runs:
            if run.user_id != row.user_id:
                raise RuntimeError(
                    f"learning run {run.id} owner differs from series {row.series_id} owner"
                )
            bind.execute(
                sa.text(
                    """
                    UPDATE learning_runs
                    SET initial_mission_version_id = :mission_id
                    WHERE id = :run_id AND initial_mission_version_id IS NULL
                    """
                ),
                {"mission_id": mission_id, "run_id": run.id},
            )
            bind.execute(
                sa.text(
                    """
                    INSERT OR IGNORE INTO mission_adoption_events (
                        id, learning_run_id, user_id, mission_version_id,
                        previous_mission_version_id, event_type, source, reason,
                        idempotency_key, created_at
                    ) VALUES (
                        :id, :run_id, :user_id, :mission_id, NULL,
                        'initialized', 'system_migration',
                        'M1 学习运行原位采用迁移任务版本', :idempotency_key,
                        :created_at
                    )
                    """
                ),
                {
                    "id": _stable_id("mission_adoption_m1", run.id, mission_id),
                    "run_id": run.id,
                    "user_id": run.user_id,
                    "mission_id": mission_id,
                    "idempotency_key": f"m1-initial:{run.id}:{mission_id}",
                    "created_at": run.created_at or created_at,
                },
            )


def upgrade():
    tables = _tables()
    if "learning_mission_versions" not in tables:
        op.create_table(
            "learning_mission_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("plan_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("why", sa.Text(), nullable=False),
            sa.Column("target_capabilities_json", sa.Text(), nullable=False),
            sa.Column("constraints_json", sa.Text(), nullable=False),
            sa.Column("out_of_scope_json", sa.Text(), nullable=False),
            sa.Column("assumptions_json", sa.Text(), nullable=False),
            sa.Column("learner_context_json", sa.Text(), nullable=False),
            sa.Column("inferred_fields_json", sa.Text(), nullable=False),
            sa.Column("provenance_json", sa.Text(), nullable=False),
            sa.Column("schema_version", sa.String(40), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["plan_id"], ["learning_plans.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(
                ["supersedes_id"], ["learning_mission_versions.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "plan_id", "version", name="uq_learning_mission_versions_plan_version"
            ),
            sa.UniqueConstraint(
                "plan_id", "payload_hash", name="uq_learning_mission_versions_plan_payload"
            ),
        )
        op.create_index(
            "ix_learning_mission_versions_plan_id",
            "learning_mission_versions",
            ["plan_id"],
        )
        op.create_index(
            "ix_learning_mission_versions_user_id",
            "learning_mission_versions",
            ["user_id"],
        )
        op.create_index(
            "ix_learning_mission_versions_status",
            "learning_mission_versions",
            ["status"],
        )

    tables = _tables()
    if "mission_success_criteria" not in tables:
        op.create_table(
            "mission_success_criteria",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("plan_id", sa.String(), nullable=False),
            sa.Column("stable_key", sa.String(160), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["plan_id"], ["learning_plans.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "plan_id", "stable_key", name="uq_mission_success_criteria_plan_key"
            ),
        )
        op.create_index(
            "ix_mission_success_criteria_plan_id",
            "mission_success_criteria",
            ["plan_id"],
        )

    tables = _tables()
    if "mission_success_criterion_versions" not in tables:
        op.create_table(
            "mission_success_criterion_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("mission_version_id", sa.String(), nullable=False),
            sa.Column("success_criterion_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("acceptance_json", sa.Text(), nullable=False),
            sa.Column("provenance_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["mission_version_id"], ["learning_mission_versions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["success_criterion_id"], ["mission_success_criteria.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "mission_version_id",
                "success_criterion_id",
                name="uq_mission_criterion_versions_identity",
            ),
            sa.UniqueConstraint(
                "mission_version_id",
                "position",
                name="uq_mission_criterion_versions_position",
            ),
        )
        op.create_index(
            "ix_mission_success_criterion_versions_mission_version_id",
            "mission_success_criterion_versions",
            ["mission_version_id"],
        )
        op.create_index(
            "ix_mission_success_criterion_versions_success_criterion_id",
            "mission_success_criterion_versions",
            ["success_criterion_id"],
        )

    if "initial_mission_version_id" not in _columns("series"):
        op.add_column(
            "series",
            sa.Column("initial_mission_version_id", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_series_initial_mission_version_id",
            "series",
            ["initial_mission_version_id"],
        )
    if "initial_mission_version_id" not in _columns("learning_runs"):
        op.add_column(
            "learning_runs",
            sa.Column("initial_mission_version_id", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_learning_runs_initial_mission_version_id",
            "learning_runs",
            ["initial_mission_version_id"],
        )

    if "mission_adoption_events" not in _tables():
        op.create_table(
            "mission_adoption_events",
            sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("learning_run_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("mission_version_id", sa.String(), nullable=False),
            sa.Column("previous_mission_version_id", sa.String(), nullable=True),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("source", sa.String(40), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(160), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["learning_run_id", "user_id"],
                ["learning_runs.id", "learning_runs.user_id"],
                name="fk_mission_adoption_events_run_user",
            ),
            sa.ForeignKeyConstraint(
                ["mission_version_id"], ["learning_mission_versions.id"]
            ),
            sa.ForeignKeyConstraint(
                ["previous_mission_version_id"], ["learning_mission_versions.id"]
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("sequence"),
            sa.UniqueConstraint("id"),
            sa.UniqueConstraint(
                "learning_run_id",
                "user_id",
                "idempotency_key",
                name="uq_mission_adoption_events_idempotency",
            ),
        )
        for column in (
            "id",
            "learning_run_id",
            "user_id",
            "mission_version_id",
            "event_type",
        ):
            op.create_index(
                f"ix_mission_adoption_events_{column}",
                "mission_adoption_events",
                [column],
                unique=(column == "id"),
            )

    _backfill_missions()


def downgrade():
    if "mission_adoption_events" in _tables():
        op.drop_table("mission_adoption_events")
    if "initial_mission_version_id" in _columns("learning_runs"):
        op.drop_index(
            "ix_learning_runs_initial_mission_version_id",
            table_name="learning_runs",
        )
        op.drop_column("learning_runs", "initial_mission_version_id")
    if "initial_mission_version_id" in _columns("series"):
        op.drop_index(
            "ix_series_initial_mission_version_id", table_name="series"
        )
        op.drop_column("series", "initial_mission_version_id")
    if "mission_success_criterion_versions" in _tables():
        op.drop_table("mission_success_criterion_versions")
    if "mission_success_criteria" in _tables():
        op.drop_table("mission_success_criteria")
    if "learning_mission_versions" in _tables():
        op.drop_table("learning_mission_versions")
