"""Add minimal platform knowledge identities and immutable learning contracts.

Revision ID: 0030_learning_contracts
Revises: 0029_learning_missions
"""

from datetime import datetime, timezone
import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "0030_learning_contracts"
down_revision = "0029_learning_missions"
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _stable_id(prefix, *parts):
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _objective_key(statement):
    normalized = " ".join(statement.strip().lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _section_objectives(question, objectives_json):
    parsed = []
    for item in _load(objectives_json, []):
        if isinstance(item, dict):
            statement = str(item.get("statement") or item.get("objective") or "").strip()
            required = item.get("required", item.get("core"))
        else:
            statement = str(item).strip()
            required = None
        if statement:
            parsed.append((statement, bool(required) if required is not None else None))
    if not parsed:
        parsed = [(str(question).strip(), True)]
    result = []
    positions = {}
    for position, (statement, explicit_required) in enumerate(parsed):
        key = _objective_key(statement)
        required = explicit_required if explicit_required is not None else position == 0
        if key in positions:
            old_statement, old_required = result[positions[key]]
            result[positions[key]] = (old_statement, old_required or required)
        else:
            positions[key] = len(result)
            result.append((statement, required))
    return result


def _create_knowledge_tables():
    if "concepts" not in _tables():
        op.create_table(
            "concepts",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("namespace", sa.String(80), nullable=False),
            sa.Column("concept_key", sa.String(200), nullable=False),
            sa.Column("canonical_name", sa.String(300), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("origin", sa.String(40), nullable=False, server_default="platform"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("namespace", "concept_key", name="uq_concepts_namespace_key"),
        )
        op.create_index("ix_concepts_namespace", "concepts", ["namespace"])
        op.create_index("ix_concepts_status", "concepts", ["status"])

    if "concept_revisions" not in _tables():
        op.create_table(
            "concept_revisions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("concept_id", sa.String(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(300), nullable=False),
            sa.Column("definition", sa.Text(), nullable=False),
            sa.Column("scope_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("boundaries_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("provenance_mode", sa.String(40), nullable=False, server_default="platform"),
            sa.Column("verification_status", sa.String(32), nullable=False, server_default="unverified"),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"]),
            sa.ForeignKeyConstraint(["supersedes_id"], ["concept_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("concept_id", "revision", name="uq_concept_revisions_concept_revision"),
        )
        op.create_index("ix_concept_revisions_concept_id", "concept_revisions", ["concept_id"])
        op.create_index("ix_concept_revisions_verification_status", "concept_revisions", ["verification_status"])

    if "learning_objectives" not in _tables():
        op.create_table(
            "learning_objectives",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("namespace", sa.String(80), nullable=False),
            sa.Column("objective_key", sa.String(200), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("cognitive_verb", sa.String(40), nullable=False, server_default="demonstrate"),
            sa.Column("outcome_type", sa.String(40), nullable=False, server_default="knowledge"),
            sa.Column("provenance_mode", sa.String(40), nullable=False, server_default="platform"),
            sa.Column("verification_status", sa.String(32), nullable=False, server_default="unverified"),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["supersedes_id"], ["learning_objectives.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("namespace", "objective_key", name="uq_learning_objectives_namespace_key"),
        )
        op.create_index("ix_learning_objectives_namespace", "learning_objectives", ["namespace"])
        op.create_index("ix_learning_objectives_verification_status", "learning_objectives", ["verification_status"])
        op.create_index("ix_learning_objectives_status", "learning_objectives", ["status"])


def _extend_assessment_targets():
    columns = _columns("assessment_targets")
    with op.batch_alter_table("assessment_targets") as batch:
        if "concept_revision_id" not in columns:
            batch.add_column(sa.Column("concept_revision_id", sa.String(), nullable=True))
            batch.create_foreign_key(
                "fk_assessment_targets_concept_revision",
                "concept_revisions",
                ["concept_revision_id"],
                ["id"],
            )
            batch.create_index(
                "ix_assessment_targets_concept_revision_id", ["concept_revision_id"]
            )
        if "learning_objective_id" not in columns:
            batch.add_column(sa.Column("learning_objective_id", sa.String(), nullable=True))
            batch.create_foreign_key(
                "fk_assessment_targets_learning_objective",
                "learning_objectives",
                ["learning_objective_id"],
                ["id"],
            )
            batch.create_index(
                "ix_assessment_targets_learning_objective_id", ["learning_objective_id"]
            )
        if "identity_status" not in columns:
            batch.add_column(
                sa.Column(
                    "identity_status",
                    sa.String(32),
                    nullable=False,
                    server_default="legacy_provisional",
                )
            )
            batch.create_index("ix_assessment_targets_identity_status", ["identity_status"])


def _create_contract_tables():
    if "learning_contract_versions" not in _tables():
        op.create_table(
            "learning_contract_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("section_id", sa.String(), nullable=False),
            sa.Column("mission_version_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("section_question_snapshot", sa.Text(), nullable=False),
            sa.Column("target_depth", sa.String(32), nullable=False, server_default="standard"),
            sa.Column("boundaries_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("generation_context_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("provenance_mode", sa.String(40), nullable=False, server_default="native_m2"),
            sa.Column("lineage_status", sa.String(32), nullable=False, server_default="verified"),
            sa.Column("contract_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
            sa.ForeignKeyConstraint(["mission_version_id"], ["learning_mission_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("section_id", "version", name="uq_learning_contract_versions_section_version"),
            sa.UniqueConstraint("section_id", "contract_hash", name="uq_learning_contract_versions_section_hash"),
        )
        op.create_index("ix_learning_contract_versions_section_id", "learning_contract_versions", ["section_id"])
        op.create_index("ix_learning_contract_versions_mission_version_id", "learning_contract_versions", ["mission_version_id"])
        op.create_index("ix_learning_contract_versions_lineage_status", "learning_contract_versions", ["lineage_status"])

    if "learning_contract_concepts" not in _tables():
        op.create_table(
            "learning_contract_concepts",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("contract_version_id", sa.String(), nullable=False),
            sa.Column("concept_revision_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(24), nullable=False, server_default="primary"),
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["contract_version_id"], ["learning_contract_versions.id"]),
            sa.ForeignKeyConstraint(["concept_revision_id"], ["concept_revisions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("contract_version_id", "concept_revision_id", name="uq_learning_contract_concepts_identity"),
            sa.UniqueConstraint("contract_version_id", "position", name="uq_learning_contract_concepts_position"),
        )
        op.create_index("ix_learning_contract_concepts_contract_version_id", "learning_contract_concepts", ["contract_version_id"])
        op.create_index("ix_learning_contract_concepts_concept_revision_id", "learning_contract_concepts", ["concept_revision_id"])

    if "learning_contract_objectives" not in _tables():
        op.create_table(
            "learning_contract_objectives",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("contract_version_id", sa.String(), nullable=False),
            sa.Column("learning_objective_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(24), nullable=False, server_default="primary"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["contract_version_id"], ["learning_contract_versions.id"]),
            sa.ForeignKeyConstraint(["learning_objective_id"], ["learning_objectives.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("contract_version_id", "learning_objective_id", name="uq_learning_contract_objectives_identity"),
            sa.UniqueConstraint("contract_version_id", "position", name="uq_learning_contract_objectives_position"),
        )
        op.create_index("ix_learning_contract_objectives_contract_version_id", "learning_contract_objectives", ["contract_version_id"])
        op.create_index("ix_learning_contract_objectives_learning_objective_id", "learning_contract_objectives", ["learning_objective_id"])

    if "learning_contract_assessment_targets" not in _tables():
        op.create_table(
            "learning_contract_assessment_targets",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("contract_version_id", sa.String(), nullable=False),
            sa.Column("assessment_target_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("verification_policy", sa.String(40), nullable=False, server_default="choice_quiz_v1"),
            sa.Column("evidence_policy", sa.String(40), nullable=False, server_default="assessment_evidence_v1"),
            sa.Column("diagnostic_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["contract_version_id"], ["learning_contract_versions.id"]),
            sa.ForeignKeyConstraint(["assessment_target_id"], ["assessment_targets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("contract_version_id", "position", name="uq_learning_contract_assessment_targets_position"),
            sa.UniqueConstraint("contract_version_id", "assessment_target_id", name="uq_learning_contract_assessment_targets_target"),
        )
        op.create_index("ix_learning_contract_assessment_targets_contract_version_id", "learning_contract_assessment_targets", ["contract_version_id"])
        op.create_index("ix_learning_contract_assessment_targets_assessment_target_id", "learning_contract_assessment_targets", ["assessment_target_id"])


def _ensure_target_identity(bind, target):
    now = datetime.now(timezone.utc)
    target_id = target["id"]
    concept_id = _stable_id("concept_m1", target_id)
    revision_id = _stable_id("concept_revision_m1", target_id, 1)
    objective_id = _stable_id("learning_objective_m1", target_id)
    bind.execute(
        sa.text(
            "INSERT OR IGNORE INTO concepts "
            "(id, namespace, concept_key, canonical_name, status, origin, created_at) "
            "VALUES (:id, 'm1_provisional', :key, :name, 'active', 'm1_provisional', :created_at)"
        ),
        {"id": concept_id, "key": target_id, "name": target["objective_statement"], "created_at": now},
    )
    bind.execute(
        sa.text(
            "INSERT OR IGNORE INTO concept_revisions "
            "(id, concept_id, revision, label, definition, scope_json, boundaries_json, "
            " provenance_mode, verification_status, supersedes_id, created_at) "
            "VALUES (:id, :concept_id, 1, :label, :definition, :scope, '[]', "
            " 'm1_provisional', 'provisional', NULL, :created_at)"
        ),
        {
            "id": revision_id,
            "concept_id": concept_id,
            "label": target["objective_statement"],
            "definition": target["objective_statement"],
            "scope": _dump({"legacyAssessmentTargetId": target_id}),
            "created_at": now,
        },
    )
    bind.execute(
        sa.text(
            "INSERT OR IGNORE INTO learning_objectives "
            "(id, namespace, objective_key, statement, cognitive_verb, outcome_type, "
            " provenance_mode, verification_status, supersedes_id, status, created_at) "
            "VALUES (:id, 'm1_provisional', :key, :statement, 'demonstrate', 'knowledge', "
            " 'm1_provisional', 'provisional', NULL, 'active', :created_at)"
        ),
        {"id": objective_id, "key": target_id, "statement": target["objective_statement"], "created_at": now},
    )
    bind.execute(
        sa.text(
            "UPDATE assessment_targets SET concept_revision_id = :revision_id, "
            "learning_objective_id = :objective_id, identity_status = 'legacy_provisional' "
            "WHERE id = :target_id"
        ),
        {"revision_id": revision_id, "objective_id": objective_id, "target_id": target_id},
    )
    return revision_id, objective_id


def _backfill_contracts():
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    for target in bind.execute(sa.text("SELECT * FROM assessment_targets ORDER BY id")).mappings():
        _ensure_target_identity(bind, target)

    sections = bind.execute(
        sa.text(
            "SELECT section.id, section.question, section.objectives_json, "
            "series.initial_mission_version_id AS mission_version_id "
            "FROM sections AS section "
            "JOIN chapters AS chapter ON chapter.id = section.chapter_id "
            "JOIN books AS book ON book.id = chapter.book_id "
            "JOIN series ON series.id = book.series_id ORDER BY section.id"
        )
    ).mappings()
    for section in sections:
        if not section["mission_version_id"]:
            raise RuntimeError(f"section {section['id']} has no mission version")
        bindings = list(
            bind.execute(
                sa.text(
                    "SELECT binding.id AS binding_id, binding.position, binding.required, "
                    "binding.verification_policy, target.* "
                    "FROM section_assessment_targets AS binding "
                    "JOIN assessment_targets AS target ON target.id = binding.assessment_target_id "
                    "WHERE binding.section_id = :section_id ORDER BY binding.position"
                ),
                {"section_id": section["id"]},
            ).mappings()
        )
        if not bindings:
            for position, (statement, required) in enumerate(
                _section_objectives(section["question"], section["objectives_json"]), 1
            ):
                key = _objective_key(statement)
                target = bind.execute(
                    sa.text(
                        "SELECT * FROM assessment_targets WHERE objective_key = :key "
                        "AND dimension = 'recognition' AND target_depth = 'standard'"
                    ),
                    {"key": key},
                ).mappings().first()
                if target is None:
                    target_id = _stable_id("target_m1", key, "recognition", "standard")
                    bind.execute(
                        sa.text(
                            "INSERT INTO assessment_targets "
                            "(id, concept_revision_id, learning_objective_id, objective_key, "
                            " objective_statement, dimension, target_depth, identity_status, status, created_at) "
                            "VALUES (:id, NULL, NULL, :key, :statement, 'recognition', 'standard', "
                            " 'legacy_provisional', 'active', :created_at)"
                        ),
                        {"id": target_id, "key": key, "statement": statement, "created_at": now},
                    )
                    target = bind.execute(
                        sa.text("SELECT * FROM assessment_targets WHERE id = :id"), {"id": target_id}
                    ).mappings().one()
                    _ensure_target_identity(bind, target)
                binding_id = _stable_id("section_target_m1", section["id"], target["id"])
                bind.execute(
                    sa.text(
                        "INSERT INTO section_assessment_targets "
                        "(id, section_id, assessment_target_id, position, required, verification_policy, created_at) "
                        "VALUES (:id, :section_id, :target_id, :position, :required, 'choice_quiz_v1', :created_at)"
                    ),
                    {
                        "id": binding_id,
                        "section_id": section["id"],
                        "target_id": target["id"],
                        "position": position,
                        "required": required,
                        "created_at": now,
                    },
                )
            bindings = list(
                bind.execute(
                    sa.text(
                        "SELECT binding.id AS binding_id, binding.position, binding.required, "
                        "binding.verification_policy, target.* "
                        "FROM section_assessment_targets AS binding "
                        "JOIN assessment_targets AS target ON target.id = binding.assessment_target_id "
                        "WHERE binding.section_id = :section_id ORDER BY binding.position"
                    ),
                    {"section_id": section["id"]},
                ).mappings()
            )

        hydrated = []
        for target in bindings:
            revision_id = target["concept_revision_id"]
            objective_id = target["learning_objective_id"]
            if not revision_id or not objective_id:
                revision_id, objective_id = _ensure_target_identity(bind, target)
            hydrated.append((target, revision_id, objective_id))
        payload = {
            "schemaVersion": "learning_contract_v1",
            "sectionId": section["id"],
            "missionVersionId": section["mission_version_id"],
            "question": section["question"],
            "targets": [
                {
                    "assessmentTargetId": target["id"],
                    "conceptRevisionId": revision_id,
                    "learningObjectiveId": objective_id,
                    "position": target["position"],
                    "required": bool(target["required"]),
                    "verificationPolicy": target["verification_policy"],
                }
                for target, revision_id, objective_id in hydrated
            ],
        }
        contract_hash = hashlib.sha256(_dump(payload).encode()).hexdigest()
        contract_id = _stable_id("learning_contract_m1", section["id"], contract_hash)
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO learning_contract_versions "
                "(id, section_id, mission_version_id, version, section_question_snapshot, "
                " target_depth, boundaries_json, generation_context_json, provenance_mode, "
                " lineage_status, contract_hash, created_at) "
                "VALUES (:id, :section_id, :mission_id, 1, :question, 'standard', '[]', "
                " :context, 'derived_from_m1', 'provisional', :contract_hash, :created_at)"
            ),
            {
                "id": contract_id,
                "section_id": section["id"],
                "mission_id": section["mission_version_id"],
                "question": section["question"],
                "context": _dump({"mode": "m1_provisional", "sourceObjectives": _load(section["objectives_json"], [])}),
                "contract_hash": contract_hash,
                "created_at": now,
            },
        )
        seen_revisions = []
        seen_objectives = []
        for target, revision_id, objective_id in hydrated:
            if revision_id not in seen_revisions:
                seen_revisions.append(revision_id)
                bind.execute(
                    sa.text(
                        "INSERT OR IGNORE INTO learning_contract_concepts "
                        "(id, contract_version_id, concept_revision_id, position, role, required, created_at) "
                        "VALUES (:id, :contract_id, :revision_id, :position, 'primary', :required, :created_at)"
                    ),
                    {
                        "id": _stable_id("contract_concept_m1", contract_id, revision_id),
                        "contract_id": contract_id,
                        "revision_id": revision_id,
                        "position": len(seen_revisions),
                        "required": target["required"],
                        "created_at": now,
                    },
                )
            if objective_id not in seen_objectives:
                seen_objectives.append(objective_id)
                bind.execute(
                    sa.text(
                        "INSERT OR IGNORE INTO learning_contract_objectives "
                        "(id, contract_version_id, learning_objective_id, position, role, created_at) "
                        "VALUES (:id, :contract_id, :objective_id, :position, 'primary', :created_at)"
                    ),
                    {
                        "id": _stable_id("contract_objective_m1", contract_id, objective_id),
                        "contract_id": contract_id,
                        "objective_id": objective_id,
                        "position": len(seen_objectives),
                        "created_at": now,
                    },
                )
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO learning_contract_assessment_targets "
                    "(id, contract_version_id, assessment_target_id, position, required, "
                    " verification_policy, evidence_policy, diagnostic_only, created_at) "
                    "VALUES (:id, :contract_id, :target_id, :position, :required, "
                    " :verification_policy, 'assessment_evidence_v1', 0, :created_at)"
                ),
                {
                    "id": _stable_id("contract_target_m1", contract_id, target["id"]),
                    "contract_id": contract_id,
                    "target_id": target["id"],
                    "position": target["position"],
                    "required": target["required"],
                    "verification_policy": target["verification_policy"],
                    "created_at": now,
                },
            )


def upgrade():
    _create_knowledge_tables()
    _extend_assessment_targets()
    _create_contract_tables()
    _backfill_contracts()


def downgrade():
    for table in (
        "learning_contract_assessment_targets",
        "learning_contract_objectives",
        "learning_contract_concepts",
        "learning_contract_versions",
    ):
        if table in _tables():
            op.drop_table(table)
    columns = _columns("assessment_targets")
    with op.batch_alter_table("assessment_targets") as batch:
        if "identity_status" in columns:
            batch.drop_index("ix_assessment_targets_identity_status")
            batch.drop_column("identity_status")
        if "learning_objective_id" in columns:
            batch.drop_index("ix_assessment_targets_learning_objective_id")
            batch.drop_constraint("fk_assessment_targets_learning_objective", type_="foreignkey")
            batch.drop_column("learning_objective_id")
        if "concept_revision_id" in columns:
            batch.drop_index("ix_assessment_targets_concept_revision_id")
            batch.drop_constraint("fk_assessment_targets_concept_revision", type_="foreignkey")
            batch.drop_column("concept_revision_id")
    for table in ("learning_objectives", "concept_revisions", "concepts"):
        if table in _tables():
            op.drop_table(table)
