import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    Capability,
    CapabilityConceptBinding,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    ConceptRevision,
    LearningObjective,
)


CAPABILITY_ROUTE_RULE_VERSION = "capability_route_v1"
CAPABILITY_RUBRIC_VERSION = "capability_rubric_v1"


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode()
    ).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_route_capability(
    db: Session,
    *,
    series_id: str,
    concept_revision_id: str,
) -> tuple[CapabilityRevision, CapabilityStageCriterion]:
    """Create the conservative route-local capability behind a concept target.

    A capability can naturally reach gold, while a route initially promises
    only its governed bronze choice task. Contract publication raises the route
    promise to silver only after both oral targets are frozen.
    """

    concept_revision = db.get(ConceptRevision, concept_revision_id)
    if concept_revision is None:
        raise AppError(
            "能力缺少有效的知识版本",
            code="CAPABILITY_CONCEPT_REVISION_MISSING",
            status=500,
        )

    namespace = f"route_capability:{series_id}"
    capability_key = concept_revision_id
    capability_id = _stable_id("capability_route", series_id, concept_revision_id)
    revision_id = _stable_id("capability_revision_route", capability_id, 1)

    capability = db.get(Capability, capability_id)
    if capability is None:
        capability = Capability(
            id=capability_id,
            namespace=namespace,
            capability_key=capability_key,
            canonical_name=f"理解并运用{concept_revision.label}",
            status="active",
            origin="route_scoped",
        )
        db.add(capability)

    revision = db.get(CapabilityRevision, revision_id)
    if revision is None:
        revision = CapabilityRevision(
            id=revision_id,
            capability_id=capability_id,
            revision=1,
            label=f"理解并运用{concept_revision.label}",
            scope_json=_dump(
                {
                    "anchorConceptRevisionId": concept_revision_id,
                    "seriesId": series_id,
                    "rubricVersion": CAPABILITY_RUBRIC_VERSION,
                }
            ),
            operation_json=_dump(
                {
                    "bronze": "recognize_and_state",
                    "silver": "explain_mechanism_and_confusions",
                    "gold": "solve_unseen_standard_task",
                    "diamond": "transfer_to_unfamiliar_context",
                }
            ),
            context_constraints_json=_dump({"routeScoped": True}),
            natural_stage_ceiling="gold",
            provenance_mode="route_scoped",
            verification_status="route_scoped",
        )
        db.add(revision)

    concept_binding_id = _stable_id(
        "capability_concept", revision_id, concept_revision_id
    )
    if db.get(CapabilityConceptBinding, concept_binding_id) is None:
        db.add(
            CapabilityConceptBinding(
                id=concept_binding_id,
                capability_revision_id=revision_id,
                concept_revision_id=concept_revision_id,
                role="anchor",
                position=1,
                required=True,
            )
        )

    criterion_specs = (
        (
            "bronze",
            1,
            f"能够辨认并说明{concept_revision.label}的核心含义",
            "choice_quiz",
            "initial_or_novel",
            "unassisted",
            "taught",
            "choice_quiz_v1",
        ),
        (
            "silver",
            1,
            f"能够解释{concept_revision.label}的关键机制与关系",
            "oral_explanation",
            "independent",
            "unassisted_oral",
            "taught_with_variation",
            "oral_explanation_v1",
        ),
        (
            "silver",
            2,
            f"能够说明{concept_revision.label}的适用边界和常见混淆",
            "oral_boundary",
            "independent",
            "unassisted_oral",
            "boundary_or_confusion",
            "oral_boundary_v1",
        ),
        (
            "gold",
            1,
            f"能够在未见过的标准任务中运用{concept_revision.label}",
            "standard_application",
            "unseen",
            "unassisted",
            "standard_novel",
            "standard_application_v1",
        ),
        (
            "diamond",
            1,
            f"能够在陌生或综合情境中迁移运用{concept_revision.label}",
            "transfer_task",
            "unseen",
            "unassisted",
            "unfamiliar_or_integrated",
            "transfer_task_v1",
        ),
    )
    criteria: dict[str, CapabilityStageCriterion] = {}
    for spec in criterion_specs:
        (
            stage,
            position,
            statement,
            task_type,
            novelty,
            assistance,
            context,
            protocol,
        ) = spec
        criterion_id = _stable_id(
            "capability_criterion", revision_id, stage, position
        )
        criterion = db.get(CapabilityStageCriterion, criterion_id)
        if criterion is None:
            criterion = CapabilityStageCriterion(
                id=criterion_id,
                capability_revision_id=revision_id,
                stage=stage,
                position=position,
                statement=statement,
                task_type=task_type,
                novelty_requirement=novelty,
                assistance_limit=assistance,
                context_requirement=context,
                required=True,
                verification_protocol=protocol,
            )
            db.add(criterion)
        criteria[stage] = criterion

    route_binding_id = _stable_id("capability_route_binding", series_id, revision_id)
    if db.get(CapabilityRouteBinding, route_binding_id) is None:
        db.add(
            CapabilityRouteBinding(
                id=route_binding_id,
                series_id=series_id,
                capability_revision_id=revision_id,
                target_stage="bronze",
                route_json=_dump(
                    {
                        "naturalStageCeiling": "gold",
                        "formalStageCeiling": "bronze",
                        "reason": "silver_requires_a_frozen_ask_me_contract",
                    }
                ),
                opportunities_json=_dump(
                    [
                        {
                            "stage": "bronze",
                            "criterionId": criteria["bronze"].id,
                            "verificationProtocol": "choice_quiz_v1",
                        }
                    ]
                ),
                status="active",
                rule_version=CAPABILITY_ROUTE_RULE_VERSION,
            )
        )

    db.flush()
    bronze = db.scalar(
        select(CapabilityStageCriterion).where(
            CapabilityStageCriterion.capability_revision_id == revision_id,
            CapabilityStageCriterion.stage == "bronze",
            CapabilityStageCriterion.position == 1,
        )
    )
    if bronze is None:
        raise AppError(
            "能力缺少青铜阶段量规",
            code="CAPABILITY_BRONZE_CRITERION_MISSING",
            status=500,
        )
    return revision, bronze


def ensure_ask_me_stage_targets(
    db: Session,
    *,
    series_id: str,
    capability_revision_id: str,
    concept_revision_id: str,
) -> dict[str, AssessmentTarget]:
    """Materialize non-gate oral targets with exact stage-criterion bindings."""

    capability_revision = db.get(CapabilityRevision, capability_revision_id)
    concept_revision = db.get(ConceptRevision, concept_revision_id)
    if capability_revision is None or concept_revision is None:
        raise AppError(
            "口试能力目标缺少稳定身份",
            code="ASK_ME_CAPABILITY_IDENTITY_MISSING",
            status=500,
        )
    criterion_rows = db.scalars(
        select(CapabilityStageCriterion).where(
            CapabilityStageCriterion.capability_revision_id
            == capability_revision_id
        )
    ).all()
    criteria = {(item.stage, item.position): item for item in criterion_rows}
    specs = {
        "mechanism": ("silver", 1, "oral_explanation_v1"),
        "boundary": ("silver", 2, "oral_boundary_v1"),
        "application": ("gold", 1, "standard_application_v1"),
        # The transfer topic remains diagnostic. Its oral protocol is not the
        # criterion's formal transfer-task protocol and cannot grant diamond.
        "transfer": ("diamond", 1, "oral_transfer_probe_v1"),
    }
    namespace = f"route_capability_target:{series_id}"
    targets: dict[str, AssessmentTarget] = {}
    for dimension, (stage, position, _protocol) in specs.items():
        criterion = criteria.get((stage, position))
        if criterion is None:
            raise AppError(
                "口试能力目标缺少阶段量规",
                code="ASK_ME_CAPABILITY_CRITERION_MISSING",
                status=500,
            )
        objective_id = _stable_id(
            "learning_objective_capability", capability_revision_id, criterion.id
        )
        if db.get(LearningObjective, objective_id) is None:
            db.add(
                LearningObjective(
                    id=objective_id,
                    namespace=namespace,
                    objective_key=f"{capability_revision_id}:{criterion.id}",
                    statement=criterion.statement,
                    cognitive_verb=(
                        "apply"
                        if dimension == "application"
                        else "explain"
                        if dimension != "transfer"
                        else "transfer"
                    ),
                    outcome_type="capability",
                    provenance_mode="route_scoped",
                    verification_status="route_scoped",
                    status="active",
                )
            )
        target_id = _stable_id(
            "target_capability_stage",
            capability_revision_id,
            criterion.id,
            dimension,
        )
        target = db.get(AssessmentTarget, target_id)
        if target is None:
            target = AssessmentTarget(
                id=target_id,
                concept_revision_id=concept_revision_id,
                learning_objective_id=objective_id,
                capability_revision_id=capability_revision_id,
                capability_stage_criterion_id=criterion.id,
                objective_key=f"capability:{capability_revision_id}:{criterion.id}",
                objective_statement=criterion.statement,
                dimension=dimension,
                target_depth="standard",
                identity_status="route_scoped_capability",
                status="active",
            )
            db.add(target)
        targets[dimension] = target

    route_binding = db.scalar(
        select(CapabilityRouteBinding).where(
            CapabilityRouteBinding.series_id == series_id,
            CapabilityRouteBinding.capability_revision_id
            == capability_revision_id,
        )
    )
    if route_binding is None:
        raise AppError(
            "口试能力目标缺少系列路线绑定",
            code="ASK_ME_CAPABILITY_ROUTE_MISSING",
            status=500,
        )
    route_binding.target_stage = "silver"
    route_binding.route_json = _dump(
        {
            "naturalStageCeiling": capability_revision.natural_stage_ceiling,
            "formalStageCeiling": "silver",
            "reason": "choice_quiz_and_two_independent_oral_criteria",
        }
    )
    route_binding.opportunities_json = _dump(
        [
            {
                "stage": "bronze",
                "criterionId": criteria[("bronze", 1)].id,
                "verificationProtocol": "choice_quiz_v1",
            },
            {
                "stage": "silver",
                "criterionId": criteria[("silver", 1)].id,
                "verificationProtocol": "oral_explanation_v1",
            },
            {
                "stage": "silver",
                "criterionId": criteria[("silver", 2)].id,
                "verificationProtocol": "oral_boundary_v1",
            },
        ]
    )
    db.flush()
    return targets


def publish_standard_application_opportunity(
    db: Session,
    *,
    series_id: str,
    capability_revision_id: str,
    task_version_id: str,
) -> CapabilityRouteBinding:
    """Raise a route ceiling only after a governed gold task is published."""

    revision = db.get(CapabilityRevision, capability_revision_id)
    route = db.scalar(
        select(CapabilityRouteBinding).where(
            CapabilityRouteBinding.series_id == series_id,
            CapabilityRouteBinding.capability_revision_id
            == capability_revision_id,
        )
    )
    criterion = db.scalar(
        select(CapabilityStageCriterion).where(
            CapabilityStageCriterion.capability_revision_id
            == capability_revision_id,
            CapabilityStageCriterion.stage == "gold",
            CapabilityStageCriterion.position == 1,
            CapabilityStageCriterion.verification_protocol
            == "standard_application_v1",
        )
    )
    if revision is None or route is None or criterion is None:
        raise AppError(
            "标准应用任务缺少能力路线绑定",
            code="CAPABILITY_APPLICATION_ROUTE_MISSING",
            status=500,
        )
    opportunities = json.loads(route.opportunities_json or "[]")
    opportunity = {
        "stage": "gold",
        "criterionId": criterion.id,
        "verificationProtocol": "standard_application_v1",
        "taskVersionId": task_version_id,
    }
    opportunities = [
        item
        for item in opportunities
        if not (
            item.get("stage") == "gold"
            and item.get("criterionId") == criterion.id
        )
    ]
    opportunities.append(opportunity)
    route.target_stage = "gold"
    route.route_json = _dump(
        {
            "naturalStageCeiling": "gold",
            "formalStageCeiling": "gold",
            "reason": "published_unseen_standard_application_task",
        }
    )
    route.opportunities_json = _dump(opportunities)
    db.flush()
    return route
