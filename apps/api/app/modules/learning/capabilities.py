import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Capability,
    CapabilityConceptBinding,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    ConceptRevision,
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

    The first rollout only promises bronze because the current route has a
    governed choice-quiz task. Higher criteria are explicit model objects, but
    remain outside the immutable route ceiling until real tasks are planned.
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
            natural_stage_ceiling="bronze",
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
            f"能够辨认并说明{concept_revision.label}的核心含义",
            "choice_quiz",
            "initial_or_novel",
            "unassisted",
            "taught",
            "choice_quiz_v1",
        ),
        (
            "silver",
            f"能够解释{concept_revision.label}的机制、关系和常见混淆",
            "oral_explanation",
            "independent",
            "unassisted_oral",
            "taught_with_variation",
            "oral_explanation_v1",
        ),
        (
            "gold",
            f"能够在未见过的标准任务中运用{concept_revision.label}",
            "standard_application",
            "unseen",
            "unassisted",
            "standard_novel",
            "standard_application_v1",
        ),
        (
            "diamond",
            f"能够在陌生或综合情境中迁移运用{concept_revision.label}",
            "transfer_task",
            "unseen",
            "unassisted",
            "unfamiliar_or_integrated",
            "transfer_task_v1",
        ),
    )
    criteria: dict[str, CapabilityStageCriterion] = {}
    for position, spec in enumerate(criterion_specs, 1):
        stage, statement, task_type, novelty, assistance, context, protocol = spec
        criterion_id = _stable_id("capability_criterion", revision_id, stage, 1)
        criterion = db.get(CapabilityStageCriterion, criterion_id)
        if criterion is None:
            criterion = CapabilityStageCriterion(
                id=criterion_id,
                capability_revision_id=revision_id,
                stage=stage,
                position=1,
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
                        "naturalStageCeiling": "bronze",
                        "reason": "choice_quiz_is_the_only_formal_task_in_v1",
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
