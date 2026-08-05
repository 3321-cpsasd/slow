"""Bind M1 learning facts to immutable contract and content versions.

Revision ID: 0031_learning_version_bindings
Revises: 0030_learning_contracts
"""

from datetime import datetime, timezone
import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "0031_learning_version_bindings"
down_revision = "0030_learning_contracts"
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name):
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}


def _stable_id(prefix, *parts):
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _add_nullable_fk(table_name, column_name, target_table, index_name):
    if column_name in _columns(table_name):
        return
    with op.batch_alter_table(table_name) as batch:
        batch.add_column(sa.Column(column_name, sa.String(), nullable=True))
        batch.create_foreign_key(
            f"fk_{table_name}_{column_name}",
            target_table,
            [column_name],
            ["id"],
        )
        batch.create_index(index_name, [column_name])


def _expand_lineage_columns():
    _add_nullable_fk(
        "content_versions",
        "learning_contract_version_id",
        "learning_contract_versions",
        "ix_content_versions_learning_contract_version_id",
    )
    _add_nullable_fk(
        "quiz_sets",
        "learning_contract_version_id",
        "learning_contract_versions",
        "ix_quiz_sets_learning_contract_version_id",
    )
    for column, target in (
        ("learning_contract_version_id", "learning_contract_versions"),
        ("content_version_id", "content_versions"),
    ):
        _add_nullable_fk(
            "quiz_attempts", column, target, f"ix_quiz_attempts_{column}"
        )
    for column, target in (
        ("quiz_set_id", "quiz_sets"),
        ("learning_contract_version_id", "learning_contract_versions"),
        ("content_version_id", "content_versions"),
    ):
        _add_nullable_fk(
            "assessment_observations",
            column,
            target,
            f"ix_assessment_observations_{column}",
        )
    for table in (
        "qa_sessions",
        "learning_notes",
        "ask_me_sessions",
        "learning_resume_positions",
    ):
        _add_nullable_fk(
            table,
            "learning_contract_version_id",
            "learning_contract_versions",
            f"ix_{table}_learning_contract_version_id",
        )
        _add_nullable_fk(
            table,
            "content_version_id",
            "content_versions",
            f"ix_{table}_content_version_id",
        )
    _add_nullable_fk(
        "learning_note_summaries",
        "learning_contract_version_id",
        "learning_contract_versions",
        "ix_learning_note_summaries_learning_contract_version_id",
    )


def _create_binding_table():
    if "learning_run_section_bindings" in _tables():
        return
    op.create_table(
        "learning_run_section_bindings",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("learning_run_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("learning_contract_version_id", sa.String(), nullable=False),
        sa.Column("content_version_id", sa.String(), nullable=False),
        sa.Column("initial_quiz_set_id", sa.String(), nullable=True),
        sa.Column("first_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_fact_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("lineage_audit_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["learning_run_id", "user_id"],
            ["learning_runs.id", "learning_runs.user_id"],
            name="fk_learning_run_section_bindings_run_user",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"]),
        sa.ForeignKeyConstraint(
            ["learning_contract_version_id"], ["learning_contract_versions.id"]
        ),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.ForeignKeyConstraint(["initial_quiz_set_id"], ["quiz_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "learning_run_id",
            "section_id",
            name="uq_learning_run_section_bindings_run_section",
        ),
    )
    for column in (
        "learning_run_id",
        "user_id",
        "section_id",
        "learning_contract_version_id",
        "content_version_id",
    ):
        op.create_index(
            f"ix_learning_run_section_bindings_{column}",
            "learning_run_section_bindings",
            [column],
        )


def _content_catalog(bind):
    by_section = {}
    block_matches = {}
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, section_id, learning_contract_version_id, version, "
                "blocks_json, created_at FROM content_versions ORDER BY section_id, version"
            )
        ).mappings()
    )
    for row in rows:
        by_section.setdefault(row["section_id"], []).append(row)
        for block in _load(row["blocks_json"], []):
            block_id = str(block.get("id", "")).strip() if isinstance(block, dict) else ""
            if block_id:
                block_matches.setdefault((row["section_id"], block_id), []).append(row)
    return by_section, block_matches


def _temporal_content(by_section, section_id, occurred_at):
    rows = by_section.get(section_id, [])
    if not rows:
        return None
    before = [
        row for row in rows if str(row["created_at"]) <= str(occurred_at)
    ]
    return (before or rows)[-1 if before else 0]


def _backfill_direct_lineage():
    bind = op.get_bind()
    missing = bind.execute(
        sa.text(
            "SELECT content.id FROM content_versions AS content "
            "LEFT JOIN learning_contract_versions AS contract "
            "ON contract.section_id = content.section_id AND contract.version = 1 "
            "WHERE contract.id IS NULL LIMIT 1"
        )
    ).scalar_one_or_none()
    if missing:
        raise RuntimeError(f"content version {missing} has no section contract v1")

    bind.execute(
        sa.text(
            "UPDATE content_versions SET learning_contract_version_id = ("
            "SELECT id FROM learning_contract_versions WHERE section_id = content_versions.section_id "
            "ORDER BY version LIMIT 1) WHERE learning_contract_version_id IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE quiz_sets SET learning_contract_version_id = ("
            "SELECT learning_contract_version_id FROM content_versions "
            "WHERE id = quiz_sets.content_version_id) WHERE learning_contract_version_id IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE quiz_attempts SET content_version_id = ("
            "SELECT content_version_id FROM quiz_sets WHERE id = quiz_attempts.quiz_set_id), "
            "learning_contract_version_id = (SELECT learning_contract_version_id FROM quiz_sets "
            "WHERE id = quiz_attempts.quiz_set_id) "
            "WHERE content_version_id IS NULL OR learning_contract_version_id IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE assessment_observations SET quiz_set_id = ("
            "SELECT quiz_set_id FROM quiz_attempts WHERE id = assessment_observations.attempt_id), "
            "content_version_id = (SELECT content_version_id FROM quiz_attempts "
            "WHERE id = assessment_observations.attempt_id), "
            "learning_contract_version_id = (SELECT learning_contract_version_id "
            "FROM quiz_attempts WHERE id = assessment_observations.attempt_id) "
            "WHERE quiz_set_id IS NULL OR content_version_id IS NULL "
            "OR learning_contract_version_id IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE learning_note_summaries SET learning_contract_version_id = ("
            "SELECT learning_contract_version_id FROM content_versions "
            "WHERE id = learning_note_summaries.source_content_version_id) "
            "WHERE source_content_version_id IS NOT NULL "
            "AND learning_contract_version_id IS NULL"
        )
    )
    return bind, _content_catalog(bind)


def _add_candidate(candidates, *, run_id, user_id, section_id, content, source,
                   fact_id, occurred_at, priority, quiz_set_id=None):
    if not content:
        return
    candidates.setdefault((run_id, section_id), []).append(
        {
            "runId": run_id,
            "userId": user_id,
            "sectionId": section_id,
            "contentVersionId": content["id"],
            "contractVersionId": content["learning_contract_version_id"],
            "quizSetId": quiz_set_id,
            "source": source,
            "factId": fact_id,
            "occurredAt": str(occurred_at),
            "priority": priority,
        }
    )


def _backfill_activity_lineage():
    bind, (by_section, block_matches) = _backfill_direct_lineage()
    candidates = {}

    attempts = list(
        bind.execute(
            sa.text(
                "SELECT attempt.id, attempt.learning_run_id, attempt.user_id, attempt.created_at, "
                "quiz.id AS quiz_set_id, quiz.section_id, content.id AS content_id, "
                "content.learning_contract_version_id AS contract_id, content.created_at AS content_created_at "
                "FROM quiz_attempts AS attempt JOIN quiz_sets AS quiz ON quiz.id = attempt.quiz_set_id "
                "JOIN content_versions AS content ON content.id = quiz.content_version_id "
                "ORDER BY attempt.created_at, attempt.id"
            )
        ).mappings()
    )
    for row in attempts:
        content = {
            "id": row["content_id"],
            "learning_contract_version_id": row["contract_id"],
            "created_at": row["content_created_at"],
        }
        _add_candidate(
            candidates,
            run_id=row["learning_run_id"],
            user_id=row["user_id"],
            section_id=row["section_id"],
            content=content,
            source="attempt",
            fact_id=row["id"],
            occurred_at=row["created_at"],
            priority=0,
            quiz_set_id=row["quiz_set_id"],
        )

    summaries = list(
        bind.execute(
            sa.text(
                "SELECT summary.id, summary.created_at, note.learning_run_id, note.user_id, "
                "note.section_id, content.id AS content_id, content.learning_contract_version_id AS contract_id, "
                "content.created_at AS content_created_at FROM learning_note_summaries AS summary "
                "JOIN learning_notes AS note ON note.id = summary.note_id "
                "JOIN content_versions AS content ON content.id = summary.source_content_version_id "
                "ORDER BY summary.created_at, summary.id"
            )
        ).mappings()
    )
    for row in summaries:
        content = {
            "id": row["content_id"],
            "learning_contract_version_id": row["contract_id"],
            "created_at": row["content_created_at"],
        }
        bind.execute(
            sa.text(
                "UPDATE learning_notes SET learning_contract_version_id = :contract_id, "
                "content_version_id = :content_id WHERE learning_run_id = :run_id "
                "AND section_id = :section_id AND learning_contract_version_id IS NULL"
            ),
            {
                "contract_id": row["contract_id"],
                "content_id": row["content_id"],
                "run_id": row["learning_run_id"],
                "section_id": row["section_id"],
            },
        )
        _add_candidate(
            candidates,
            run_id=row["learning_run_id"], user_id=row["user_id"],
            section_id=row["section_id"], content=content,
            source="note_summary", fact_id=row["id"],
            occurred_at=row["created_at"], priority=1,
        )

    qa_rows = list(
        bind.execute(
            sa.text(
                "SELECT session.id AS session_id, session.learning_run_id, session.user_id, "
                "session.section_id, message.id AS message_id, message.block_id, message.created_at "
                "FROM qa_sessions AS session JOIN qa_messages AS message ON message.session_id = session.id "
                "ORDER BY message.created_at, message.id"
            )
        ).mappings()
    )
    seen_qa = set()
    for row in qa_rows:
        if row["session_id"] in seen_qa:
            continue
        matches = block_matches.get((row["section_id"], row["block_id"]), [])
        if len(matches) != 1:
            continue
        content = matches[0]
        seen_qa.add(row["session_id"])
        bind.execute(
            sa.text(
                "UPDATE qa_sessions SET learning_contract_version_id = :contract_id, "
                "content_version_id = :content_id WHERE id = :id"
            ),
            {"contract_id": content["learning_contract_version_id"], "content_id": content["id"], "id": row["session_id"]},
        )
        _add_candidate(
            candidates, run_id=row["learning_run_id"], user_id=row["user_id"],
            section_id=row["section_id"], content=content, source="qa_block",
            fact_id=row["message_id"], occurred_at=row["created_at"], priority=2,
        )

    resumes = list(bind.execute(sa.text("SELECT * FROM learning_resume_positions ORDER BY updated_at, id")).mappings())
    for row in resumes:
        matches = block_matches.get((row["section_id"], row["block_id"]), [])
        if len(matches) != 1:
            continue
        content = matches[0]
        bind.execute(
            sa.text(
                "UPDATE learning_resume_positions SET learning_contract_version_id = :contract_id, "
                "content_version_id = :content_id WHERE id = :id"
            ),
            {"contract_id": content["learning_contract_version_id"], "content_id": content["id"], "id": row["id"]},
        )
        _add_candidate(
            candidates, run_id=row["learning_run_id"], user_id=row["user_id"],
            section_id=row["section_id"], content=content, source="resume_block",
            fact_id=row["id"], occurred_at=row["updated_at"], priority=3,
        )

    ask_me_rows = list(bind.execute(sa.text("SELECT * FROM ask_me_sessions ORDER BY created_at, id")).mappings())
    for row in ask_me_rows:
        content = _temporal_content(by_section, row["section_id"], row["created_at"])
        if not content:
            continue
        bind.execute(
            sa.text(
                "UPDATE ask_me_sessions SET learning_contract_version_id = :contract_id, "
                "content_version_id = :content_id WHERE id = :id"
            ),
            {"contract_id": content["learning_contract_version_id"], "content_id": content["id"], "id": row["id"]},
        )
        _add_candidate(
            candidates, run_id=row["learning_run_id"], user_id=row["user_id"],
            section_id=row["section_id"], content=content, source="ask_me_temporal",
            fact_id=row["id"], occurred_at=row["created_at"], priority=4,
        )

    completed = list(
        bind.execute(
            sa.text(
                "SELECT * FROM section_progress WHERE status = 'completed' "
                "ORDER BY updated_at, id"
            )
        ).mappings()
    )
    for row in completed:
        content = _temporal_content(by_section, row["section_id"], row["updated_at"])
        _add_candidate(
            candidates, run_id=row["learning_run_id"], user_id=row["user_id"],
            section_id=row["section_id"], content=content, source="completed_temporal",
            fact_id=row["id"], occurred_at=row["updated_at"], priority=5,
        )

    now = datetime.now(timezone.utc)
    for (_run_id, _section_id), items in candidates.items():
        ordered = sorted(
            items,
            key=lambda item: (item["priority"], str(item["occurredAt"]), item["factId"]),
        )
        chosen = ordered[0]
        initial_quiz_id = chosen["quizSetId"]
        if not initial_quiz_id:
            initial_quiz_id = bind.execute(
                sa.text(
                    "SELECT quiz.id FROM quiz_sets AS quiz LEFT JOIN remediations AS remediation "
                    "ON remediation.replacement_quiz_id = quiz.id "
                    "WHERE quiz.content_version_id = :content_id AND remediation.id IS NULL "
                    "ORDER BY quiz.generation, quiz.id LIMIT 1"
                ),
                {"content_id": chosen["contentVersionId"]},
            ).scalar_one_or_none()
        audit = {
            "ruleVersion": "m1_binding_v1",
            "selection": "source_priority_then_earliest_real_activity",
            "candidates": items,
            "conflictingContentVersionIds": sorted(
                {item["contentVersionId"] for item in items}
            ),
        }
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO learning_run_section_bindings "
                "(id, learning_run_id, user_id, section_id, learning_contract_version_id, "
                "content_version_id, initial_quiz_set_id, first_read_at, source, source_fact_id, "
                "lineage_audit_json, created_at) VALUES (:id, :run_id, :user_id, :section_id, "
                ":contract_id, :content_id, :quiz_id, :first_read_at, :source, :fact_id, :audit, :created_at)"
            ),
            {
                "id": _stable_id("run_section_binding_m1", chosen["runId"], chosen["sectionId"]),
                "run_id": chosen["runId"], "user_id": chosen["userId"],
                "section_id": chosen["sectionId"], "contract_id": chosen["contractVersionId"],
                "content_id": chosen["contentVersionId"], "quiz_id": initial_quiz_id,
                "first_read_at": chosen["occurredAt"], "source": chosen["source"],
                "fact_id": chosen["factId"], "audit": _dump(audit), "created_at": now,
            },
        )
        if chosen["source"] == "attempt":
            bind.execute(
                sa.text(
                    "UPDATE learning_notes SET learning_contract_version_id = :contract_id, "
                    "content_version_id = :content_id WHERE learning_run_id = :run_id "
                    "AND section_id = :section_id AND learning_contract_version_id IS NULL"
                ),
                {
                    "contract_id": chosen["contractVersionId"],
                    "content_id": chosen["contentVersionId"],
                    "run_id": chosen["runId"], "section_id": chosen["sectionId"],
                },
            )


def upgrade():
    _expand_lineage_columns()
    _create_binding_table()
    _backfill_activity_lineage()


def downgrade():
    if "learning_run_section_bindings" in _tables():
        op.drop_table("learning_run_section_bindings")
    columns = {
        "learning_note_summaries": ["learning_contract_version_id"],
        "learning_resume_positions": ["content_version_id", "learning_contract_version_id"],
        "ask_me_sessions": ["content_version_id", "learning_contract_version_id"],
        "learning_notes": ["content_version_id", "learning_contract_version_id"],
        "qa_sessions": ["content_version_id", "learning_contract_version_id"],
        "assessment_observations": ["content_version_id", "learning_contract_version_id", "quiz_set_id"],
        "quiz_attempts": ["content_version_id", "learning_contract_version_id"],
        "quiz_sets": ["learning_contract_version_id"],
        "content_versions": ["learning_contract_version_id"],
    }
    for table, names in columns.items():
        existing = _columns(table)
        with op.batch_alter_table(table) as batch:
            for name in names:
                if name in existing:
                    batch.drop_index(f"ix_{table}_{name}")
                    batch.drop_constraint(f"fk_{table}_{name}", type_="foreignkey")
                    batch.drop_column(name)
