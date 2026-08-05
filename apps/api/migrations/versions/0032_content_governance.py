"""Normalize M1 content and add claim-level governance lineage.

Revision ID: 0032_content_governance
Revises: 0031_learning_version_bindings
"""

from datetime import datetime, timezone
import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "0032_content_governance"
down_revision = "0031_learning_version_bindings"
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def _stable_id(prefix, *parts):
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _hash(*parts):
    material = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(material.encode()).hexdigest()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _create_tables():
    if "source_versions" not in _tables():
        op.create_table(
            "source_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("content_version_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("source_kind", sa.String(32), nullable=False),
            sa.Column("version_label", sa.String(200), nullable=False, server_default=""),
            sa.Column("provenance_mode", sa.String(40), nullable=False, server_default="native_m2"),
            sa.Column("reachability_status", sa.String(32), nullable=False, server_default="unknown"),
            sa.Column("verification_report_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("content_version_id", "position", name="uq_source_versions_content_position"),
        )
        op.create_index("ix_source_versions_content_version_id", "source_versions", ["content_version_id"])
        op.create_index("ix_source_versions_provenance_mode", "source_versions", ["provenance_mode"])
        op.create_index("ix_source_versions_reachability_status", "source_versions", ["reachability_status"])

    if "content_block_versions" not in _tables():
        op.create_table(
            "content_block_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("content_version_id", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("block_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("format_kind", sa.String(24), nullable=False),
            sa.Column("semantic_role", sa.String(32), nullable=False),
            sa.Column("heading", sa.Text(), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("source_indexes_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("factuality_class", sa.String(40), nullable=False, server_default="unspecified"),
            sa.Column("trust_state", sa.String(32), nullable=False, server_default="model_synthesis"),
            sa.Column("generation_method", sa.String(40), nullable=False, server_default="ai_generated"),
            sa.Column("assessment_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
            sa.ForeignKeyConstraint(["supersedes_id"], ["content_block_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("content_version_id", "position", name="uq_content_block_versions_content_position"),
        )
        op.create_index("ix_content_block_versions_content_version_id", "content_block_versions", ["content_version_id"])
        op.create_index("ix_content_block_versions_semantic_role", "content_block_versions", ["semantic_role"])
        op.create_index("ix_content_block_versions_trust_state", "content_block_versions", ["trust_state"])

    if "source_claims" not in _tables():
        op.create_table(
            "source_claims",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("stable_key", sa.String(64), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("stable_key"),
        )
        op.create_index("ix_source_claims_stable_key", "source_claims", ["stable_key"], unique=True)
        op.create_index("ix_source_claims_status", "source_claims", ["status"])

    if "source_claim_versions" not in _tables():
        op.create_table(
            "source_claim_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("source_claim_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("claim_kind", sa.String(40), nullable=False),
            sa.Column("scope_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("strict", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("trust_state", sa.String(32), nullable=False, server_default="unverified"),
            sa.Column("generation_method", sa.String(40), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="candidate"),
            sa.Column("supersedes_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["source_claim_id"], ["source_claims.id"]),
            sa.ForeignKeyConstraint(["supersedes_id"], ["source_claim_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_claim_id", "version", name="uq_source_claim_versions_claim_version"),
        )
        op.create_index("ix_source_claim_versions_source_claim_id", "source_claim_versions", ["source_claim_id"])
        op.create_index("ix_source_claim_versions_claim_kind", "source_claim_versions", ["claim_kind"])
        op.create_index("ix_source_claim_versions_strict", "source_claim_versions", ["strict"])
        op.create_index("ix_source_claim_versions_trust_state", "source_claim_versions", ["trust_state"])
        op.create_index("ix_source_claim_versions_status", "source_claim_versions", ["status"])

    if "content_block_claim_anchors" not in _tables():
        op.create_table(
            "content_block_claim_anchors",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("content_block_version_id", sa.String(), nullable=False),
            sa.Column("source_claim_version_id", sa.String(), nullable=False),
            sa.Column("anchor_role", sa.String(32), nullable=False, server_default="states"),
            sa.Column("locator_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["content_block_version_id"], ["content_block_versions.id"]),
            sa.ForeignKeyConstraint(["source_claim_version_id"], ["source_claim_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("content_block_version_id", "source_claim_version_id", name="uq_content_block_claim_anchors_identity"),
        )
        op.create_index("ix_content_block_claim_anchors_content_block_version_id", "content_block_claim_anchors", ["content_block_version_id"])
        op.create_index("ix_content_block_claim_anchors_source_claim_version_id", "content_block_claim_anchors", ["source_claim_version_id"])

    if "source_claim_bindings" not in _tables():
        op.create_table(
            "source_claim_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("source_claim_version_id", sa.String(), nullable=False),
            sa.Column("source_version_id", sa.String(), nullable=False),
            sa.Column("locator_type", sa.String(32), nullable=False),
            sa.Column("locator_json", sa.Text(), nullable=False),
            sa.Column("locator_hash", sa.String(64), nullable=False),
            sa.Column("excerpt_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("excerpt_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("support_type", sa.String(24), nullable=False),
            sa.Column("verification_mode", sa.String(32), nullable=False),
            sa.Column("verification_status", sa.String(32), nullable=False),
            sa.Column("verification_rule_version", sa.String(40), nullable=False),
            sa.Column("report_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["source_claim_version_id"], ["source_claim_versions.id"]),
            sa.ForeignKeyConstraint(["source_version_id"], ["source_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_claim_version_id", "source_version_id", "locator_hash", name="uq_source_claim_bindings_claim_source_locator"),
        )
        op.create_index("ix_source_claim_bindings_source_claim_version_id", "source_claim_bindings", ["source_claim_version_id"])
        op.create_index("ix_source_claim_bindings_source_version_id", "source_claim_bindings", ["source_version_id"])
        op.create_index("ix_source_claim_bindings_verification_status", "source_claim_bindings", ["verification_status"])

    if "knowledge_gaps" not in _tables():
        op.create_table(
            "knowledge_gaps",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("gap_type", sa.String(40), nullable=False),
            sa.Column("severity", sa.String(24), nullable=False),
            sa.Column("subject_kind", sa.String(40), nullable=False),
            sa.Column("source_claim_version_id", sa.String(), nullable=True),
            sa.Column("content_version_id", sa.String(), nullable=True),
            sa.Column("content_block_version_id", sa.String(), nullable=True),
            sa.Column("detector_kind", sa.String(32), nullable=False),
            sa.Column("detector_rule_version", sa.String(40), nullable=False),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["source_claim_version_id"], ["source_claim_versions.id"]),
            sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
            sa.ForeignKeyConstraint(["content_block_version_id"], ["content_block_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        for column in ("gap_type", "severity", "source_claim_version_id", "content_version_id", "content_block_version_id"):
            op.create_index(f"ix_knowledge_gaps_{column}", "knowledge_gaps", [column])

    if "knowledge_gap_events" not in _tables():
        op.create_table(
            "knowledge_gap_events",
            sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("knowledge_gap_id", sa.String(), nullable=False),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("actor_kind", sa.String(32), nullable=False),
            sa.Column("actor_id", sa.String(160), nullable=False, server_default=""),
            sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("rule_version", sa.String(40), nullable=False),
            sa.Column("idempotency_key", sa.String(160), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["knowledge_gap_id"], ["knowledge_gaps.id"]),
            sa.PrimaryKeyConstraint("sequence"),
            sa.UniqueConstraint("id"),
            sa.UniqueConstraint("knowledge_gap_id", "idempotency_key", name="uq_knowledge_gap_events_idempotency"),
        )
        op.create_index("ix_knowledge_gap_events_id", "knowledge_gap_events", ["id"], unique=True)
        op.create_index("ix_knowledge_gap_events_knowledge_gap_id", "knowledge_gap_events", ["knowledge_gap_id"])
        op.create_index("ix_knowledge_gap_events_event_type", "knowledge_gap_events", ["event_type"])

    if "governance_decision_snapshots" not in _tables():
        op.create_table(
            "governance_decision_snapshots",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("decision_scope", sa.String(32), nullable=False),
            sa.Column("content_version_id", sa.String(), nullable=False),
            sa.Column("quiz_set_id", sa.String(), nullable=True),
            sa.Column("learning_contract_version_id", sa.String(), nullable=True),
            sa.Column("requested_mode", sa.String(24), nullable=False),
            sa.Column("mode", sa.String(24), nullable=False),
            sa.Column("allowed", sa.Boolean(), nullable=False),
            sa.Column("assessment_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("rule_version", sa.String(40), nullable=False),
            sa.Column("input_hash", sa.String(64), nullable=False),
            sa.Column("actor_kind", sa.String(32), nullable=False),
            sa.Column("actor_id", sa.String(160), nullable=False, server_default=""),
            sa.Column("idempotency_key", sa.String(160), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
            sa.ForeignKeyConstraint(["quiz_set_id"], ["quiz_sets.id"]),
            sa.ForeignKeyConstraint(["learning_contract_version_id"], ["learning_contract_versions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("decision_scope", "idempotency_key", name="uq_governance_decision_scope_idempotency"),
        )
        for column in ("decision_scope", "content_version_id", "quiz_set_id", "learning_contract_version_id", "mode"):
            op.create_index(f"ix_governance_decision_snapshots_{column}", "governance_decision_snapshots", [column])


def _claim_kind(block):
    role = str(block.get("role", "")).strip()
    declared = str(block.get("claimKind") or block.get("claim_kind") or "").strip()
    assessable = bool(
        block.get("assessmentEligible")
        or block.get("assessment_eligible")
        or block.get("assessable")
    )
    factuality = str(
        block.get("factualityClass") or block.get("factuality_class") or ""
    ).strip()
    relationship = block.get("knowledgeRelation") or block.get("knowledge_relation")
    if declared in {
        "core_conclusion",
        "assessable_fact",
        "version_sensitive_fact",
        "boundary",
        "key_knowledge_relation",
    }:
        return declared
    if role == "conclusion" or block.get("core") is True:
        return "core_conclusion"
    if role == "boundary":
        return "boundary"
    if assessable:
        return "assessable_fact"
    if factuality == "version_sensitive_fact":
        return "version_sensitive_fact"
    if relationship:
        return "key_knowledge_relation"
    return None


def _verification_catalog(bind):
    if "source_verifications" not in _tables():
        return {}
    result = {}
    rows = bind.execute(
        sa.text("SELECT content_version_id, report_json FROM source_verifications")
    ).mappings()
    for row in rows:
        by_url = {}
        for item in _load(row["report_json"], []):
            if isinstance(item, dict):
                by_url[str(item.get("url", ""))] = item
        result[row["content_version_id"]] = by_url
    return result


def _backfill():
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    verification_catalog = _verification_catalog(bind)
    contents = bind.execute(
        sa.text(
            "SELECT id, blocks_json, sources_json, created_at FROM content_versions "
            "ORDER BY section_id, version, id"
        )
    ).mappings()
    for content_row in contents:
        content_id = content_row["id"]
        created_at = content_row["created_at"] or now
        sources = _load(content_row["sources_json"], [])
        source_ids = {}
        reports_by_url = verification_catalog.get(content_id, {})
        for position, source in enumerate(sources):
            if not isinstance(source, dict):
                continue
            source_id = _stable_id("source_version_m1", content_id, position)
            source_ids[position] = source_id
            url = str(source.get("url", ""))
            report = reports_by_url.get(url, {})
            reachability = str(report.get("verificationStatus") or "unknown")
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO source_versions "
                    "(id, content_version_id, position, title, url, source_kind, version_label, "
                    "provenance_mode, reachability_status, verification_report_json, created_at) "
                    "VALUES (:id,:content_id,:position,:title,:url,:kind,:version,"
                    "'derived_from_m1',:reachability,:report,:created_at)"
                ),
                {
                    "id": source_id,
                    "content_id": content_id,
                    "position": position,
                    "title": str(source.get("title", "")),
                    "url": url,
                    "kind": str(source.get("kind", "unknown")),
                    "version": str(source.get("version", "")),
                    "reachability": reachability,
                    "report": _dump(report),
                    "created_at": created_at,
                },
            )

        blocks = _load(content_row["blocks_json"], [])
        for position, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            original_id = str(block.get("id", "")).strip()
            block_id = original_id or _stable_id("block_m1_missing_id", content_id, position)
            collision = bind.execute(
                sa.text(
                    "SELECT content_version_id FROM content_block_versions WHERE id=:id"
                ),
                {"id": block_id},
            ).scalar_one_or_none()
            if collision and collision != content_id:
                raise RuntimeError(
                    f"legacy block id {block_id} belongs to multiple content versions"
                )
            claim_kind = _claim_kind(block)
            source_indexes = [
                index
                for index in block.get("source_indexes", block.get("sourceIndexes", []))
                if isinstance(index, int)
            ]
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO content_block_versions "
                    "(id, content_version_id, position, block_version, format_kind, semantic_role, "
                    "heading, content, source_indexes_json, factuality_class, trust_state, "
                    "generation_method, assessment_eligible, supersedes_id, created_at) VALUES "
                    "(:id,:content_id,:position,:block_version,:format_kind,:role,:heading,:content,"
                    ":source_indexes,:factuality,:trust_state,'derived_from_m1',:assessment,NULL,:created_at)"
                ),
                {
                    "id": block_id,
                    "content_id": content_id,
                    "position": position,
                    "block_version": int(block.get("version", 1) or 1),
                    "format_kind": str(block.get("kind", "text")),
                    "role": str(block.get("role", "transition")),
                    "heading": str(block.get("heading", "")),
                    "content": str(block.get("content", "")),
                    "source_indexes": _dump(source_indexes),
                    "factuality": str(block.get("factualityClass") or block.get("factuality_class") or "legacy_unspecified"),
                    "trust_state": "legacy_unverified" if claim_kind else "model_synthesis",
                    "assessment": bool(
                        block.get("assessmentEligible")
                        or block.get("assessment_eligible")
                        or block.get("assessable")
                    ),
                    "created_at": created_at,
                },
            )
            if not claim_kind:
                continue

            stable_key = _hash("m1_block_claim", content_id, block_id, claim_kind)
            claim_id = _stable_id("source_claim_m1", stable_key)
            claim_version_id = _stable_id("source_claim_version_m1", claim_id, 1)
            statement = str(block.get("content") or block.get("heading") or block_id)
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO source_claims (id,stable_key,status,created_at) "
                    "VALUES (:id,:stable_key,'active',:created_at)"
                ),
                {"id": claim_id, "stable_key": stable_key, "created_at": created_at},
            )
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO source_claim_versions "
                    "(id,source_claim_id,version,statement,claim_kind,scope_json,strict,trust_state,"
                    "generation_method,status,supersedes_id,created_at) VALUES "
                    "(:id,:claim_id,1,:statement,:kind,:scope,1,'legacy_unverified',"
                    "'derived_from_m1','candidate',NULL,:created_at)"
                ),
                {
                    "id": claim_version_id,
                    "claim_id": claim_id,
                    "statement": statement,
                    "kind": claim_kind,
                    "scope": _dump({"contentVersionId": content_id, "legacyBlockId": block_id}),
                    "created_at": created_at,
                },
            )
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO content_block_claim_anchors "
                    "(id,content_block_version_id,source_claim_version_id,anchor_role,locator_json,created_at) "
                    "VALUES (:id,:block_id,:claim_version_id,'states',:locator,:created_at)"
                ),
                {
                    "id": _stable_id("block_claim_anchor_m1", block_id, claim_version_id),
                    "block_id": block_id,
                    "claim_version_id": claim_version_id,
                    "locator": _dump({"kind": "whole_legacy_block"}),
                    "created_at": created_at,
                },
            )

            binding_count = 0
            for source_index in source_indexes:
                source_id = source_ids.get(source_index)
                if not source_id:
                    continue
                locator = _dump({"legacySourceIndex": source_index})
                locator_hash = _hash(locator)
                source_report = reports_by_url.get(
                    str(sources[source_index].get("url", "")), {}
                )
                bind.execute(
                    sa.text(
                        "INSERT OR IGNORE INTO source_claim_bindings "
                        "(id,source_claim_version_id,source_version_id,locator_type,locator_json,"
                        "locator_hash,excerpt_text,excerpt_hash,support_type,verification_mode,"
                        "verification_status,verification_rule_version,report_json,verified_at,created_at) "
                        "VALUES (:id,:claim_version_id,:source_id,'legacy_source_index',:locator,"
                        ":locator_hash,'','','candidate_support','legacy_migration','legacy_unverified',"
                        "'m1_claim_import_v1',:report,NULL,:created_at)"
                    ),
                    {
                        "id": _stable_id("claim_binding_m1", claim_version_id, source_id, locator_hash),
                        "claim_version_id": claim_version_id,
                        "source_id": source_id,
                        "locator": locator,
                        "locator_hash": locator_hash,
                        "report": _dump(
                            {
                                "sourceReachability": source_report.get("verificationStatus", "unknown"),
                                "warning": "source_reachability_is_not_claim_support",
                            }
                        ),
                        "created_at": created_at,
                    },
                )
                binding_count += 1

            gap_id = _stable_id("knowledge_gap_m1", claim_version_id, "unsupported_claim")
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO knowledge_gaps "
                    "(id,gap_type,severity,subject_kind,source_claim_version_id,content_version_id,"
                    "content_block_version_id,detector_kind,detector_rule_version,details_json,detected_at) "
                    "VALUES (:id,'unsupported_claim','blocking','source_claim_version',:claim_version_id,"
                    ":content_id,:block_id,'system_migration','m1_governance_backfill_v1',:details,:detected_at)"
                ),
                {
                    "id": gap_id,
                    "claim_version_id": claim_version_id,
                    "content_id": content_id,
                    "block_id": block_id,
                    "details": _dump(
                        {
                            "legacyCandidateBindingCount": binding_count,
                            "reason": "M1 source indexes do not prove claim-level support",
                        }
                    ),
                    "detected_at": now,
                },
            )
            bind.execute(
                sa.text(
                    "INSERT OR IGNORE INTO knowledge_gap_events "
                    "(id,knowledge_gap_id,event_type,actor_kind,actor_id,rationale,evidence_json,"
                    "rule_version,idempotency_key,created_at) VALUES "
                    "(:id,:gap_id,'opened','system_migration','','M1 strict claim requires claim-level verification',"
                    "'{}','m1_governance_backfill_v1',:idempotency_key,:created_at)"
                ),
                {
                    "id": _stable_id("knowledge_gap_event_m1", gap_id, "opened"),
                    "gap_id": gap_id,
                    "idempotency_key": f"opened:{gap_id}",
                    "created_at": now,
                },
            )


def upgrade():
    _create_tables()
    _backfill()


def downgrade():
    for table in (
        "governance_decision_snapshots",
        "knowledge_gap_events",
        "knowledge_gaps",
        "source_claim_bindings",
        "content_block_claim_anchors",
        "source_claim_versions",
        "source_claims",
        "content_block_versions",
        "source_versions",
    ):
        if table in _tables():
            op.drop_table(table)
