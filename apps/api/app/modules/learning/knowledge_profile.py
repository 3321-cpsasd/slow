"""Evidence-only learner profile rebuilt from lower-level projections."""

from __future__ import annotations

import json
from collections import Counter
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...infrastructure.tables import (
    AssessmentObservation,
    KnowledgeNodeStateProjection,
    LearnerKnowledgeProfileProjection,
    now,
)


LEARNER_KNOWLEDGE_PROFILE_RULE_VERSION = "learner_knowledge_profile_v1"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def rebuild_learner_knowledge_profile(
    db: Session,
    *,
    user_id: str,
) -> LearnerKnowledgeProfileProjection:
    nodes = db.scalars(
        select(KnowledgeNodeStateProjection).where(
            KnowledgeNodeStateProjection.user_id == user_id
        )
    ).all()
    rank_counts = Counter(item.current_rank for item in nodes)
    activation_counts = Counter(item.activation_state for item in nodes)
    evidence_count = sum(item.evidence_count for item in nodes)
    independent_count = sum(item.independent_evidence_count for item in nodes)
    watermark = int(db.scalar(
        select(func.max(AssessmentObservation.sequence)).where(
            AssessmentObservation.user_id == user_id
        )
    ) or 0)
    strongest = sorted(
        nodes,
        key=lambda item: (
            -item.current_rank_order,
            -item.current_stars,
            item.concept_revision_id,
        ),
    )[:8]
    summary = {
        "schemaVersion": "learner_knowledge_profile_summary_v1",
        "nodeCount": len(nodes),
        "rankedNodeCount": sum(item.current_rank_order > 0 for item in nodes),
        "activeNodeCount": activation_counts.get("active", 0),
        "needsAttentionNodeCount": (
            activation_counts.get("due", 0)
            + activation_counts.get("reassessment", 0)
        ),
        "rankCounts": dict(sorted(rank_counts.items())),
        "activationCounts": dict(sorted(activation_counts.items())),
        "strongestEvidenceBackedNodes": [
            {
                "conceptRevisionId": item.concept_revision_id,
                "rank": item.current_rank,
                "rankOrder": item.current_rank_order,
                "stars": item.current_stars,
                "activation": item.activation_state,
            }
            for item in strongest
        ],
        "basis": "qualified_assessment_evidence_only",
    }
    row = db.scalar(
        select(LearnerKnowledgeProfileProjection).where(
            LearnerKnowledgeProfileProjection.user_id == user_id
        )
    )
    if row is None:
        row = LearnerKnowledgeProfileProjection(
            id=f"learner_knowledge_profile_{uuid4().hex}",
            user_id=user_id,
        )
        db.add(row)
    else:
        row.projection_version = (row.projection_version or 0) + 1
    row.summary_json = _dump(summary)
    row.evidence_count = evidence_count
    row.independent_evidence_count = independent_count
    row.profile_rule_version = LEARNER_KNOWLEDGE_PROFILE_RULE_VERSION
    row.source_observation_watermark = watermark
    row.rebuilt_at = now()
    row.updated_at = now()
    db.flush()
    return row


def learner_knowledge_profile_view(db: Session, *, user_id: str) -> dict:
    row = db.scalar(
        select(LearnerKnowledgeProfileProjection).where(
            LearnerKnowledgeProfileProjection.user_id == user_id
        )
    )
    if row is None:
        row = rebuild_learner_knowledge_profile(db, user_id=user_id)
    return {
        **_load(row.summary_json, {}),
        "evidenceCount": row.evidence_count,
        "independentEvidenceCount": row.independent_evidence_count,
        "profileRuleVersion": row.profile_rule_version,
        "projectionVersion": row.projection_version,
        "sourceObservationWatermark": row.source_observation_watermark,
    }
