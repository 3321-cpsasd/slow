"""Evidence-backed, rebuildable user ranks for published knowledge nodes."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    ConceptRevision,
    KnowledgeNodeStateProjection,
    KnowledgeStateProjection,
    ReviewState,
    now,
)


KNOWLEDGE_RANK_RULE_VERSION = "knowledge_rank_v3"
KNOWLEDGE_RANK_POLICY_VERSION = "knowledge_rank_policy_v1"
RANK_SETTLEABLE_IDENTITY_STATUSES = (
    "published_knowledge_graph",
    "route_scoped_knowledge",
)
RANK_SETTLEABLE_REVISION_STATUSES = ("reviewed", "route_scoped")

RANKS = {
    "unranked": (0, "尚未验证", "还没有足够证据形成能力判断"),
    "bronze": (1, "青铜 · 了解", "能够辨认并说明核心含义"),
    "silver": (2, "白银 · 理解", "能够解释关键机制与关系"),
    "gold": (3, "黄金 · 会用", "能够独立解决标准问题"),
    "platinum": (4, "铂金 · 熟练", "能够处理变化、反例和适用边界"),
    "diamond": (5, "钻石 · 迁移", "能够用于陌生或综合情境"),
    "master": (6, "大师 · 稳固", "跨时间和不同情境仍能稳定调用"),
}


def validate_rank_policy_payload(raw: object) -> dict | None:
    """Normalize one node-local rank rubric or fail closed.

    The same validator is used at graph-publication time and projection time so
    a concept cannot be published under one interpretation and ranked under
    another.
    """

    if not isinstance(raw, dict):
        return None
    if raw.get("version") != KNOWLEDGE_RANK_POLICY_VERSION:
        return None
    capability_scope = str(raw.get("capabilityScope") or "").strip()
    rank_ceiling = str(raw.get("rankCeiling") or "").strip()
    raw_dimension_ranks = raw.get("dimensionRanks")
    if (
        not capability_scope
        or rank_ceiling not in RANKS
        or rank_ceiling == "unranked"
        or not isinstance(raw_dimension_ranks, dict)
        or not raw_dimension_ranks
    ):
        return None
    ceiling_order = RANKS[rank_ceiling][0]
    dimension_ranks = {
        str(dimension): str(rank)
        for dimension, rank in raw_dimension_ranks.items()
        if str(dimension).strip()
        and str(rank) in RANKS
        and str(rank) not in {"unranked", "master"}
        and RANKS[str(rank)][0] <= ceiling_order
    }
    if len(dimension_ranks) != len(raw_dimension_ranks):
        return None
    required_top_evidence = "diamond" if rank_ceiling == "master" else rank_ceiling
    if required_top_evidence not in dimension_ranks.values():
        return None
    return {
        "version": KNOWLEDGE_RANK_POLICY_VERSION,
        "capabilityScope": capability_scope,
        "rankCeiling": rank_ceiling,
        "dimensionRanks": dimension_ranks,
    }


def rank_policy_for_revision(revision: ConceptRevision) -> dict | None:
    """Load the immutable, node-local capability contract.

    A rank is maturity relative to one precisely scoped capability, not a
    universal measure of subject difficulty. Formal nodes therefore fail
    closed unless their immutable revision declares which assessment
    dimensions prove which local milestone and where the node's natural rank
    ceiling sits.
    """

    scope = _load(revision.scope_json, {})
    raw = scope.get("rankPolicy") if isinstance(scope, dict) else None
    return validate_rank_policy_payload(raw)


# Private alias retained for older internal callers while publication and read
# models use the explicit public boundary above.
_rank_policy = rank_policy_for_revision


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _dimension(observation: AssessmentObservation, target: AssessmentTarget) -> str:
    payload_dimension = str(_load(observation.payload_json, {}).get("dimension") or "")
    return payload_dimension or target.dimension or "recognition"


def _signature(observation: AssessmentObservation) -> str:
    """Return one independent rank context, never one signature per question.

    Qualification rejects unauthorized repeats and same-session remediation.
    Grouping the remaining evidence by episode and dimension prevents a quiz
    containing several similar items from manufacturing several stars at once.
    """

    return (
        f"episode:{observation.learning_episode_id}:"
        f"{str(_load(observation.payload_json, {}).get('dimension') or '')}"
    )


def _rank_for_evidence(
    observations: list[AssessmentObservation],
    targets: dict[str, AssessmentTarget],
    retention_rounds: int,
    policy: dict,
) -> tuple[str, int, int]:
    successful = [item for item in observations if item.correct]
    if not successful:
        return "unranked", 0, 0
    ranked = [
        (
            RANKS[
                policy["dimensionRanks"][
                    _dimension(item, targets[item.assessment_target_id])
                ]
            ][0],
            item,
        )
        for item in successful
    ]
    strongest_order = max(order for order, _ in ranked)
    strongest_rank = next(
        rank for rank, (order, _label, _meaning) in RANKS.items()
        if order == strongest_order
    )
    if (
        strongest_rank == "diamond"
        and policy["rankCeiling"] == "master"
        and retention_rounds >= 2
    ):
        return (
            "master",
            min(3, retention_rounds),
            len({_signature(item) for item in successful}),
        )
    strongest_signatures = {
        _signature(item)
        for order, item in ranked
        if order == strongest_order
    }
    return (
        strongest_rank,
        min(3, len(strongest_signatures)),
        len({_signature(item) for item in successful}),
    )


def rebuild_knowledge_node_projections(
    db: Session,
    *,
    user_id: str,
    observations: list[AssessmentObservation],
    rank_observation_ids: set[str],
) -> int:
    target_ids = {item.assessment_target_id for item in observations}
    targets = (
        {
            item.id: item
            for item in db.scalars(
                select(AssessmentTarget).where(AssessmentTarget.id.in_(target_ids))
            ).all()
        }
        if target_ids
        else {}
    )
    concept_ids = {
        target.concept_revision_id
        for target in targets.values()
        if target.concept_revision_id
        and target.identity_status in RANK_SETTLEABLE_IDENTITY_STATUSES
    }
    rankable_revisions = (
        {
            item.id: item
            for item in db.scalars(
                select(ConceptRevision).where(
                    ConceptRevision.id.in_(concept_ids),
                    ConceptRevision.verification_status.in_(
                        RANK_SETTLEABLE_REVISION_STATUSES
                    ),
                )
            ).all()
        }
        if concept_ids
        else {}
    )
    policies = {
        concept_id: policy
        for concept_id, revision in rankable_revisions.items()
        if (policy := rank_policy_for_revision(revision)) is not None
    }
    by_concept: dict[str, list[AssessmentObservation]] = defaultdict(list)
    for observation in observations:
        target = targets.get(observation.assessment_target_id)
        if (
            observation.id in rank_observation_ids
            and target
            and target.concept_revision_id in policies
            and _dimension(observation, target)
            in policies[target.concept_revision_id]["dimensionRanks"]
        ):
            by_concept[target.concept_revision_id].append(observation)

    target_states = (
        {
            item.assessment_target_id: item
            for item in db.scalars(
                select(KnowledgeStateProjection).where(
                    KnowledgeStateProjection.user_id == user_id,
                    KnowledgeStateProjection.assessment_target_id.in_(target_ids),
                )
            ).all()
        }
        if target_ids
        else {}
    )
    reviews = (
        {
            item.assessment_target_id: item
            for item in db.scalars(
                select(ReviewState).where(
                    ReviewState.user_id == user_id,
                    ReviewState.assessment_target_id.in_(target_ids),
                )
            ).all()
        }
        if target_ids
        else {}
    )
    existing = {
        item.concept_revision_id: item
        for item in db.scalars(
            select(KnowledgeNodeStateProjection).where(
                KnowledgeNodeStateProjection.user_id == user_id
            )
        ).all()
    }

    current_time = now()
    for concept_id, items in by_concept.items():
        concept_target_ids = {
            item.assessment_target_id
            for item in items
            if item.assessment_target_id in targets
        }
        retention_rounds = max(
            (
                target_states[target_id].retention_rounds
                for target_id in concept_target_ids
                if target_id in target_states
            ),
            default=0,
        )
        rank, stars, independent_count = _rank_for_evidence(
            items, targets, retention_rounds, policies[concept_id]
        )
        rank_order = RANKS[rank][0]
        relevant_reviews = [
            reviews[target_id]
            for target_id in concept_target_ids
            if target_id in reviews and reviews[target_id].next_due_at is not None
        ]
        next_due_at = min(
            (
                item.next_due_at
                for item in relevant_reviews
                if item.next_due_at is not None
            ),
            default=None,
        )
        latest = max(items, key=lambda item: (_utc(item.created_at), item.sequence))
        activation_state = (
            "reassessment"
            if not latest.correct
            else "learning"
            if rank == "unranked"
            else "due"
            if next_due_at is not None and _utc(next_due_at) <= _utc(current_time)
            else "active"
        )
        uncertainties = [
            target_states[target_id].uncertainty_ppm
            for target_id in concept_target_ids
            if target_id in target_states
        ]
        row = existing.pop(concept_id, None)
        if row is None:
            row = KnowledgeNodeStateProjection(
                id=_uid("knowledge_node_state"),
                user_id=user_id,
                concept_revision_id=concept_id,
            )
            db.add(row)
        else:
            row.projection_version = (row.projection_version or 0) + 1
        row.current_rank = rank
        row.current_rank_order = rank_order
        row.current_stars = stars
        row.highest_rank = rank
        row.highest_rank_order = rank_order
        row.highest_stars = stars
        row.activation_state = activation_state
        row.stability_days = [1, 3, 7, 14][min(retention_rounds, 3)]
        row.next_due_at = next_due_at
        row.last_qualified_at = latest.created_at
        row.evidence_count = len(items)
        row.independent_evidence_count = independent_count
        row.uncertainty_ppm = (
            round(sum(uncertainties) / len(uncertainties))
            if uncertainties
            else 1_000_000
        )
        row.rank_rule_version = KNOWLEDGE_RANK_RULE_VERSION
        row.source_observation_watermark = max(item.sequence for item in items)
        row.rebuilt_at = current_time
        row.updated_at = current_time

    for stale in existing.values():
        db.delete(stale)
    db.flush()
    return len(by_concept)


def _view(
    revision: ConceptRevision,
    state: KnowledgeNodeStateProjection | None,
    policy: dict,
) -> dict:
    rank = state.current_rank if state else "unranked"
    order, rank_label, meaning = RANKS.get(rank, RANKS["unranked"])
    activation = state.activation_state if state else "learning"
    if (
        state
        and state.next_due_at is not None
        and _utc(state.next_due_at) <= _utc(now())
        and activation == "active"
    ):
        activation = "due"
    rank_ceiling = policy["rankCeiling"]
    return {
        "conceptRevisionId": revision.id,
        "label": revision.label,
        "rank": rank,
        "rankOrder": order,
        "rankLabel": rank_label,
        "meaning": meaning,
        "capabilityScope": policy["capabilityScope"],
        "rankPolicyVersion": policy["version"],
        "rankCeiling": rank_ceiling,
        "rankCeilingLabel": RANKS[rank_ceiling][1],
        "atCeiling": order == RANKS[rank_ceiling][0],
        "stars": state.current_stars if state else 0,
        "highestRank": state.highest_rank if state else "unranked",
        "highestStars": state.highest_stars if state else 0,
        "activation": activation,
        "stabilityDays": state.stability_days if state else 1,
        "nextDueAt": (
            state.next_due_at.isoformat()
            if state and state.next_due_at
            else None
        ),
        "evidenceCount": state.evidence_count if state else 0,
        "independentEvidenceCount": state.independent_evidence_count if state else 0,
        "rankRuleVersion": (
            state.rank_rule_version if state else KNOWLEDGE_RANK_RULE_VERSION
        ),
        "sourceObservationWatermark": state.source_observation_watermark if state else 0,
    }


def knowledge_node_views_for_targets(
    db: Session,
    *,
    user_id: str,
    target_ids: set[str],
) -> dict[str, dict]:
    if not target_ids:
        return {}
    concept_ids = set(
        db.scalars(
            select(AssessmentTarget.concept_revision_id).where(
                AssessmentTarget.id.in_(target_ids),
                AssessmentTarget.identity_status.in_(
                    RANK_SETTLEABLE_IDENTITY_STATUSES
                ),
                AssessmentTarget.concept_revision_id.is_not(None),
            )
        ).all()
    )
    revisions = (
        db.scalars(
            select(ConceptRevision).where(
                ConceptRevision.id.in_(concept_ids),
                ConceptRevision.verification_status.in_(
                    RANK_SETTLEABLE_REVISION_STATUSES
                ),
            )
        ).all()
        if concept_ids
        else []
    )
    policies = {
        revision.id: policy
        for revision in revisions
        if (policy := rank_policy_for_revision(revision)) is not None
    }
    states = (
        {
            item.concept_revision_id: item
            for item in db.scalars(
                select(KnowledgeNodeStateProjection).where(
                    KnowledgeNodeStateProjection.user_id == user_id,
                    KnowledgeNodeStateProjection.concept_revision_id.in_(concept_ids),
                )
            ).all()
        }
        if concept_ids
        else {}
    )
    return {
        revision.id: _view(revision, states.get(revision.id), policies[revision.id])
        for revision in revisions
        if revision.id in policies
    }


def knowledge_node_views_for_concepts(
    db: Session,
    *,
    user_id: str,
    concept_revision_ids: set[str],
) -> dict[str, dict]:
    """Read a bounded set of published node states without widening identity."""

    if not concept_revision_ids:
        return {}
    revisions = db.scalars(
        select(ConceptRevision).where(
            ConceptRevision.id.in_(concept_revision_ids),
            ConceptRevision.verification_status.in_(
                RANK_SETTLEABLE_REVISION_STATUSES
            ),
        )
    ).all()
    policies = {
        revision.id: policy
        for revision in revisions
        if (policy := rank_policy_for_revision(revision)) is not None
    }
    states = {
        item.concept_revision_id: item
        for item in db.scalars(
            select(KnowledgeNodeStateProjection).where(
                KnowledgeNodeStateProjection.user_id == user_id,
                KnowledgeNodeStateProjection.concept_revision_id.in_(
                    concept_revision_ids
                ),
            )
        ).all()
    }
    return {
        revision.id: _view(revision, states.get(revision.id), policies[revision.id])
        for revision in revisions
        if revision.id in policies
    }


def knowledge_settlement(
    before: dict[str, dict],
    after: dict[str, dict],
) -> dict:
    updates = []
    for concept_id in sorted(set(before) | set(after)):
        previous = before.get(concept_id) or {
            **after[concept_id],
            "rank": "unranked",
            "rankOrder": 0,
            "rankLabel": RANKS["unranked"][1],
            "meaning": RANKS["unranked"][2],
            "stars": 0,
            "activation": "learning",
            "evidenceCount": 0,
            "independentEvidenceCount": 0,
        }
        current = after.get(concept_id) or previous
        if current["rankOrder"] > previous["rankOrder"]:
            change = "rank_up"
            message = f"你已经证明自己{current['meaning']}。"
        elif current["stars"] > previous["stars"]:
            change = "star_up"
            message = "这次独立验证为当前能力增加了一份新证据。"
        elif current["activation"] == "reassessment":
            change = "needs_reinforcement"
            message = "这次验证暴露了仍需巩固的部分，已有最高记录会继续保留。"
        elif previous["activation"] == "due" and current["activation"] == "active":
            change = "reactivated"
            message = "这项知识已经重新恢复到可随时调用的状态。"
        else:
            change = "confirmed"
            message = "本次表现再次确认了当前能力，没有重复计算同一份成长。"
        updates.append(
            {
                "conceptRevisionId": concept_id,
                "label": current["label"],
                "before": previous,
                "after": current,
                "change": change,
                "message": message,
            }
        )
    return {
        "ruleVersion": KNOWLEDGE_RANK_RULE_VERSION,
        "updates": updates,
    }
