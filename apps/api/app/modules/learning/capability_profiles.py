import json
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    ReviewState,
    now,
)


CAPABILITY_PROJECTION_RULE_VERSION = "capability_stage_v1"
STAGE_ORDER = {
    "unranked": 0,
    "bronze": 1,
    "silver": 2,
    "gold": 3,
    "diamond": 4,
}
ORDERED_STAGES = ("bronze", "silver", "gold", "diamond")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def rebuild_capability_state_projections(
    db: Session,
    *,
    user_id: str,
    observations: list[AssessmentObservation],
    capability_observation_ids: set[str],
) -> int:
    """Project cumulative stages, evidence maturity and activation separately."""

    target_ids = {item.assessment_target_id for item in observations}
    targets = {
        item.id: item
        for item in (
            db.scalars(
                select(AssessmentTarget).where(AssessmentTarget.id.in_(target_ids))
            ).all()
            if target_ids
            else []
        )
    }
    eligible_by_capability: dict[str, list[AssessmentObservation]] = defaultdict(list)
    criterion_by_observation: dict[str, str] = {}
    for observation in observations:
        target = targets.get(observation.assessment_target_id)
        if (
            observation.id not in capability_observation_ids
            or not observation.correct
            or target is None
            or not target.capability_revision_id
            or not target.capability_stage_criterion_id
        ):
            continue
        eligible_by_capability[target.capability_revision_id].append(observation)
        criterion_by_observation[observation.id] = (
            target.capability_stage_criterion_id
        )

    existing = {
        item.capability_revision_id: item
        for item in db.scalars(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.user_id == user_id
            )
        ).all()
    }
    capability_ids = set(eligible_by_capability)
    criteria_by_capability: dict[str, list[CapabilityStageCriterion]] = defaultdict(list)
    if capability_ids:
        for criterion in db.scalars(
            select(CapabilityStageCriterion)
            .where(
                CapabilityStageCriterion.capability_revision_id.in_(capability_ids)
            )
            .order_by(
                CapabilityStageCriterion.capability_revision_id,
                CapabilityStageCriterion.position,
                CapabilityStageCriterion.id,
            )
        ).all():
            criteria_by_capability[criterion.capability_revision_id].append(criterion)
    revisions = {
        item.id: item
        for item in (
            db.scalars(
                select(CapabilityRevision).where(
                    CapabilityRevision.id.in_(capability_ids)
                )
            ).all()
            if capability_ids
            else []
        )
    }
    route_ceilings = {
        item.capability_revision_id: item.target_stage
        for item in db.scalars(
            select(CapabilityRouteBinding).where(
                CapabilityRouteBinding.capability_revision_id.in_(capability_ids),
                CapabilityRouteBinding.status == "active",
            )
        ).all()
    } if capability_ids else {}
    reviews = {
        item.assessment_target_id: item
        for item in db.scalars(
            select(ReviewState).where(ReviewState.user_id == user_id)
        ).all()
    }

    projected = 0
    as_of = now()
    for capability_id, items in eligible_by_capability.items():
        revision = revisions.get(capability_id)
        if revision is None:
            continue
        formal_ceiling = route_ceilings.get(
            capability_id, revision.natural_stage_ceiling
        )
        ceiling_order = min(
            STAGE_ORDER.get(revision.natural_stage_ceiling, 0),
            STAGE_ORDER.get(formal_ceiling, 0),
        )
        criteria = [
            criterion
            for criterion in criteria_by_capability.get(capability_id, [])
            if criterion.required
            and STAGE_ORDER.get(criterion.stage, 99) <= ceiling_order
        ]
        evidenced_criterion_ids = {
            criterion_by_observation[item.id] for item in items
        }
        satisfied_ids = sorted(
            criterion.id
            for criterion in criteria
            if criterion.id in evidenced_criterion_ids
        )

        current_stage = "unranked"
        for stage in ORDERED_STAGES:
            if STAGE_ORDER[stage] > ceiling_order:
                break
            required_at_stage = [item for item in criteria if item.stage == stage]
            if not required_at_stage or any(
                item.id not in evidenced_criterion_ids for item in required_at_stage
            ):
                break
            current_stage = stage

        missing_ids = sorted(
            criterion.id
            for criterion in criteria
            if criterion.id not in evidenced_criterion_ids
        )
        independent_keys = {item.learning_episode_id for item in items}
        delayed_items = [
            item for item in items if item.assistance_mode == "unassisted_review"
        ]
        source_types = sorted({item.source_type for item in items})
        episode_ids = sorted({item.learning_episode_id for item in items})
        maturity = {
            "evidenceCount": len(items),
            "independentEvidenceCount": len(independent_keys),
            "learningEpisodeCount": len(episode_ids),
            "delayedEvidenceCount": len(delayed_items),
            "sourceTypes": source_types,
        }

        related_reviews = [
            reviews[target.id]
            for target in targets.values()
            if target.capability_revision_id == capability_id
            and target.id in reviews
        ]
        due_dates = [item.next_due_at for item in related_reviews if item.next_due_at]
        next_due_at = min(due_dates, key=_utc) if due_dates else None
        activation_state = (
            "due_for_reactivation"
            if next_due_at is not None and _utc(next_due_at) <= _utc(as_of)
            else "available"
            if current_stage != "unranked"
            else "learning"
        )
        evidence_times = sorted(_utc(item.created_at) for item in items)
        stability_days = (
            max(0, (evidence_times[-1] - evidence_times[0]).days)
            if len(evidence_times) > 1
            else 0
        )
        watermark = max(item.sequence for item in items)
        last_qualified_at = max(items, key=lambda item: _utc(item.created_at)).created_at

        projection = existing.pop(capability_id, None)
        if projection is None:
            projection = CapabilityStateProjection(
                id=_uid("capability_state"),
                user_id=user_id,
                capability_revision_id=capability_id,
                projection_version=0,
            )
            db.add(projection)
        projection.current_stage = current_stage
        projection.current_stage_order = STAGE_ORDER[current_stage]
        projection.highest_stage = current_stage
        projection.highest_stage_order = STAGE_ORDER[current_stage]
        projection.satisfied_criterion_ids_json = _dump(satisfied_ids)
        projection.missing_criterion_ids_json = _dump(missing_ids)
        projection.evidence_maturity_json = _dump(maturity)
        projection.activation_state = activation_state
        projection.stability_days = stability_days
        projection.next_due_at = next_due_at
        projection.last_qualified_at = last_qualified_at
        projection.evidence_count = len(items)
        projection.independent_evidence_count = len(independent_keys)
        projection.projection_rule_version = CAPABILITY_PROJECTION_RULE_VERSION
        projection.projection_version += 1
        projection.source_observation_watermark = watermark
        projection.rebuilt_at = as_of
        projection.updated_at = as_of
        projected += 1

    for stale in existing.values():
        db.delete(stale)
    db.flush()
    return projected
