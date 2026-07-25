"""backfill stable anchors for content created before block ids were persisted

Revision ID: 0005_backfill_content_block_ids
Revises: 0004_repair_remediation_strategy
"""

import json

from alembic import op
import sqlalchemy as sa

revision = "0005_backfill_content_block_ids"
down_revision = "0004_repair_remediation_strategy"
branch_labels = None
depends_on = None


def _load_blocks(raw):
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def upgrade():
    connection = op.get_bind()

    content_rows = connection.execute(
        sa.text("SELECT id, version, blocks_json FROM content_versions")
    ).mappings()
    for row in content_rows:
        blocks = _load_blocks(row["blocks_json"])
        if blocks is None:
            continue
        changed = False
        for position, block in enumerate(blocks, 1):
            if not isinstance(block, dict):
                continue
            if not block.get("id"):
                block["id"] = f"block_{row['id']}_{position}"
                changed = True
            if not block.get("version"):
                block["version"] = row["version"]
                changed = True
        if changed:
            connection.execute(
                sa.text(
                    "UPDATE content_versions SET blocks_json = :blocks_json WHERE id = :id"
                ),
                {"id": row["id"], "blocks_json": json.dumps(blocks, ensure_ascii=False)},
            )

    remediation_rows = connection.execute(
        sa.text(
            "SELECT id, replacement_quiz_id, blocks_json FROM remediations"
        )
    ).mappings()
    for row in remediation_rows:
        blocks = _load_blocks(row["blocks_json"])
        if blocks is None:
            continue
        changed = False
        for position, block in enumerate(blocks, 1):
            if not isinstance(block, dict):
                continue
            if not block.get("id"):
                block["id"] = (
                    f"block_remediation_{row['replacement_quiz_id']}_{position}"
                )
                changed = True
            if not block.get("version"):
                block["version"] = 1
                changed = True
        if changed:
            connection.execute(
                sa.text("UPDATE remediations SET blocks_json = :blocks_json WHERE id = :id"),
                {"id": row["id"], "blocks_json": json.dumps(blocks, ensure_ascii=False)},
            )


def downgrade():
    # Stable block ids may already be referenced by QA evidence, so removing them
    # would break auditability. This data repair is intentionally irreversible.
    pass
