import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    AssessmentTargetConceptBinding,
    AssessmentTargetRelationBinding,
    Capability,
    CapabilityConceptBinding,
    CapabilityRelationRequirement,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    CapabilitySubnet,
    ConceptRevision,
    KnowledgeNetworkConceptBinding,
    KnowledgeNetworkRelationBinding,
    KnowledgeRelationRevision,
    LearningObjective,
)
from ..knowledge.networks import (
    KnowledgeRelationSpec,
    freeze_knowledge_network,
    validate_knowledge_network,
)


CAPABILITY_ROUTE_RULE_VERSION = "capability_route_v2_subnet"
CAPABILITY_RUBRIC_VERSION = "capability_rubric_v2_subnet"
CAPABILITY_SUBNET_RULE_VERSION = "capability_subnet_v1"


@dataclass(frozen=True)
class CapabilityConceptSpec:
    concept_revision_id: str
    role: str
    required: bool = True


@dataclass(frozen=True)
class CapabilityRelationSpec:
    from_concept_revision_id: str
    to_concept_revision_id: str
    relation_type: str
    statement: str
    minimum_stage: str = "silver"
    purpose: str = "explain"
    required: bool = True
    scope: dict | None = None
    provenance: dict | None = None


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode()
    ).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _capability_subnet_hash(
    *,
    capability_revision_id: str,
    knowledge_network_revision_id: str,
    concepts: list[dict],
    relations: list[dict],
    boundary: dict,
    context: dict,
) -> str:
    payload = {
        "ruleVersion": CAPABILITY_SUBNET_RULE_VERSION,
        "capabilityRevisionId": capability_revision_id,
        "knowledgeNetworkRevisionId": knowledge_network_revision_id,
        "concepts": concepts,
        "relations": relations,
        "boundary": boundary,
        "context": context,
    }
    return hashlib.sha256(_dump(payload).encode()).hexdigest()


def validate_capability_subnet(
    db: Session, *, capability_revision_id: str
) -> CapabilitySubnet:
    """Fail closed unless a capability references one closed, connected subnet."""

    subnet = db.scalar(
        select(CapabilitySubnet).where(
            CapabilitySubnet.capability_revision_id == capability_revision_id
        )
    )
    if subnet is None or subnet.status != "frozen":
        raise AppError(
            "能力缺少冻结的知识子网",
            code="CAPABILITY_SUBNET_MISSING",
            status=500,
        )
    validate_knowledge_network(
        db,
        knowledge_network_revision_id=subnet.knowledge_network_revision_id,
    )
    network_concept_ids = set(
        db.scalars(
            select(KnowledgeNetworkConceptBinding.concept_revision_id).where(
                KnowledgeNetworkConceptBinding.knowledge_network_revision_id
                == subnet.knowledge_network_revision_id
            )
        ).all()
    )
    concept_rows = db.scalars(
        select(CapabilityConceptBinding)
        .where(
            CapabilityConceptBinding.capability_revision_id
            == capability_revision_id
        )
        .order_by(CapabilityConceptBinding.position)
    ).all()
    if not concept_rows or sum(item.role == "anchor" for item in concept_rows) != 1:
        raise AppError(
            "能力子网必须包含且只能包含一个锚点知识",
            code="CAPABILITY_SUBNET_ANCHOR_INVALID",
            status=500,
        )
    capability_concept_ids = {item.concept_revision_id for item in concept_rows}
    if not capability_concept_ids.issubset(network_concept_ids):
        raise AppError(
            "能力子网引用了知识网络之外的节点",
            code="CAPABILITY_SUBNET_CONCEPT_OUTSIDE_NETWORK",
            status=500,
        )

    network_relation_ids = set(
        db.scalars(
            select(
                KnowledgeNetworkRelationBinding.knowledge_relation_revision_id
            ).where(
                KnowledgeNetworkRelationBinding.knowledge_network_revision_id
                == subnet.knowledge_network_revision_id
            )
        ).all()
    )
    requirement_rows = db.scalars(
        select(CapabilityRelationRequirement)
        .where(
            CapabilityRelationRequirement.capability_revision_id
            == capability_revision_id
        )
        .order_by(CapabilityRelationRequirement.position)
    ).all()
    relation_ids = {
        item.knowledge_relation_revision_id for item in requirement_rows
    }
    if not relation_ids.issubset(network_relation_ids):
        raise AppError(
            "能力引用了知识网络之外的关系",
            code="CAPABILITY_SUBNET_RELATION_OUTSIDE_NETWORK",
            status=500,
        )
    relation_revisions = {
        item.id: item
        for item in db.scalars(
            select(KnowledgeRelationRevision).where(
                KnowledgeRelationRevision.id.in_(relation_ids)
            )
        ).all()
    }
    if len(relation_revisions) != len(relation_ids):
        raise AppError(
            "能力子网存在失效的关系版本",
            code="CAPABILITY_SUBNET_RELATION_MISSING",
            status=500,
        )

    required_nodes = {
        item.concept_revision_id
        for item in concept_rows
        if item.required and item.role in {"anchor", "required"}
    }
    adjacency = {item: set() for item in required_nodes}
    for requirement in requirement_rows:
        relation = relation_revisions[requirement.knowledge_relation_revision_id]
        if (
            relation.from_concept_revision_id not in capability_concept_ids
            or relation.to_concept_revision_id not in capability_concept_ids
        ):
            raise AppError(
                "能力关系端点不属于能力子网",
                code="CAPABILITY_SUBNET_RELATION_ENDPOINT_INVALID",
                status=500,
            )
        if requirement.required:
            left = relation.from_concept_revision_id
            right = relation.to_concept_revision_id
            if left in adjacency and right in adjacency:
                adjacency[left].add(right)
                adjacency[right].add(left)
    if len(required_nodes) > 1:
        visited: set[str] = set()
        pending = [next(iter(required_nodes))]
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            pending.extend(adjacency[node] - visited)
        if visited != required_nodes:
            raise AppError(
                "能力的必需知识没有被必需关系连接成子网",
                code="CAPABILITY_SUBNET_REQUIRED_GRAPH_DISCONNECTED",
                status=500,
            )

    concepts_payload = [
        {
            "conceptRevisionId": item.concept_revision_id,
            "role": item.role,
            "required": item.required,
            "position": item.position,
        }
        for item in concept_rows
    ]
    relations_payload = [
        {
            "knowledgeRelationRevisionId": item.knowledge_relation_revision_id,
            "role": item.role,
            "required": item.required,
            "minimumStage": item.minimum_stage,
            "purpose": item.purpose,
            "position": item.position,
        }
        for item in requirement_rows
    ]
    boundary = json.loads(subnet.boundary_json or "{}")
    context = json.loads(subnet.context_json or "{}")
    expected_hash = _capability_subnet_hash(
        capability_revision_id=capability_revision_id,
        knowledge_network_revision_id=subnet.knowledge_network_revision_id,
        concepts=concepts_payload,
        relations=relations_payload,
        boundary=boundary,
        context=context,
    )
    if expected_hash != subnet.content_hash:
        raise AppError(
            "能力知识子网内容与冻结版本不一致",
            code="CAPABILITY_SUBNET_HASH_MISMATCH",
            status=500,
        )
    return subnet


def ensure_route_capability_subnet(
    db: Session,
    *,
    series_id: str,
    label: str,
    concepts: list[CapabilityConceptSpec] | tuple[CapabilityConceptSpec, ...],
    relations: list[CapabilityRelationSpec]
    | tuple[CapabilityRelationSpec, ...] = (),
    boundary: dict | None = None,
    context: dict | None = None,
    natural_stage_ceiling: str = "gold",
) -> tuple[CapabilityRevision, CapabilityStageCriterion]:
    """Create the route capability only after freezing its exact knowledge subnet."""

    concept_specs = tuple(concepts)
    relation_specs = tuple(relations)
    if not concept_specs or len(
        {item.concept_revision_id for item in concept_specs}
    ) != len(concept_specs):
        raise AppError(
            "能力必须绑定非重复的知识版本",
            code="CAPABILITY_SUBNET_CONCEPTS_INVALID",
            status=500,
        )
    if any(item.role not in {"anchor", "required", "supporting"} for item in concept_specs):
        raise AppError(
            "能力知识节点角色无效",
            code="CAPABILITY_SUBNET_CONCEPT_ROLE_INVALID",
            status=500,
        )
    if sum(item.role == "anchor" for item in concept_specs) != 1:
        raise AppError(
            "能力必须且只能指定一个锚点知识",
            code="CAPABILITY_SUBNET_ANCHOR_INVALID",
            status=500,
        )
    if any(item.role == "supporting" and item.required for item in concept_specs):
        raise AppError(
            "支撑知识不能静默成为必需考核目标",
            code="CAPABILITY_SUBNET_SUPPORTING_REQUIRED",
            status=500,
        )
    stage_order = {"bronze": 1, "silver": 2, "gold": 3, "diamond": 4}
    if natural_stage_ceiling not in stage_order:
        raise AppError(
            "能力自然上限无效",
            code="CAPABILITY_STAGE_CEILING_INVALID",
            status=500,
        )
    if any(item.minimum_stage not in stage_order for item in relation_specs):
        raise AppError(
            "能力关系的最低验证阶段无效",
            code="CAPABILITY_RELATION_STAGE_INVALID",
            status=500,
        )

    namespace = f"route_knowledge_network:{series_id}"
    frozen_network = freeze_knowledge_network(
        db,
        namespace=namespace,
        label=label,
        concept_revision_ids=[item.concept_revision_id for item in concept_specs],
        relations=[
            KnowledgeRelationSpec(
                from_concept_revision_id=item.from_concept_revision_id,
                to_concept_revision_id=item.to_concept_revision_id,
                relation_type=item.relation_type,
                statement=item.statement,
                scope=item.scope,
                provenance=item.provenance,
            )
            for item in relation_specs
        ],
        boundary=boundary,
        status="route_scoped",
        provenance_mode="route_scoped",
    )
    semantic_payload = {
        "ruleVersion": CAPABILITY_SUBNET_RULE_VERSION,
        "rubricVersion": CAPABILITY_RUBRIC_VERSION,
        "label": " ".join(label.split()),
        "concepts": [
            {
                "conceptRevisionId": item.concept_revision_id,
                "role": item.role,
                "required": item.required,
            }
            for item in concept_specs
        ],
        "relations": [
            {
                "relationRevisionId": relation.id,
                "minimumStage": spec.minimum_stage,
                "purpose": spec.purpose,
                "required": spec.required,
            }
            for spec, relation in zip(
                relation_specs, frozen_network.relation_revisions, strict=True
            )
        ],
        "boundary": boundary or {},
        "context": context or {},
        "naturalStageCeiling": natural_stage_ceiling,
    }
    semantic_hash = hashlib.sha256(_dump(semantic_payload).encode()).hexdigest()
    capability_namespace = f"route_capability:{series_id}"
    capability_id = _stable_id(
        "capability_route_subnet", series_id, semantic_hash
    )
    revision_id = _stable_id(
        "capability_revision_subnet", capability_id, 1
    )
    capability = db.get(Capability, capability_id)
    if capability is None:
        db.add(
            Capability(
                id=capability_id,
                namespace=capability_namespace,
                capability_key=semantic_hash[:40],
                canonical_name=label,
                status="active",
                origin="route_scoped",
            )
        )
        db.flush()
    revision = db.get(CapabilityRevision, revision_id)
    if revision is None:
        revision = CapabilityRevision(
            id=revision_id,
            capability_id=capability_id,
            revision=1,
            label=label,
            scope_json=_dump(
                {
                    "knowledgeNetworkRevisionId": frozen_network.revision.id,
                    "subnetRuleVersion": CAPABILITY_SUBNET_RULE_VERSION,
                    "rubricVersion": CAPABILITY_RUBRIC_VERSION,
                }
            ),
            operation_json=_dump(
                {
                    "bronze": "recognize_and_state",
                    "silver": "explain_mechanism_and_relations",
                    "gold": "solve_unseen_standard_task",
                    "diamond": "transfer_to_unfamiliar_context",
                }
            ),
            context_constraints_json=_dump(context or {}),
            natural_stage_ceiling=natural_stage_ceiling,
            provenance_mode="route_scoped",
            verification_status="route_scoped",
        )
        db.add(revision)
        db.flush()

    concept_payload: list[dict] = []
    for position, item in enumerate(concept_specs, start=1):
        binding_id = _stable_id(
            "capability_concept", revision_id, item.concept_revision_id
        )
        if db.get(CapabilityConceptBinding, binding_id) is None:
            db.add(
                CapabilityConceptBinding(
                    id=binding_id,
                    capability_revision_id=revision_id,
                    concept_revision_id=item.concept_revision_id,
                    role=item.role,
                    position=position,
                    required=item.required,
                )
            )
        concept_payload.append(
            {
                "conceptRevisionId": item.concept_revision_id,
                "role": item.role,
                "required": item.required,
                "position": position,
            }
        )

    relation_payload: list[dict] = []
    for position, (spec, relation) in enumerate(
        zip(relation_specs, frozen_network.relation_revisions, strict=True), start=1
    ):
        requirement_id = _stable_id(
            "capability_relation_requirement", revision_id, relation.id
        )
        if db.get(CapabilityRelationRequirement, requirement_id) is None:
            db.add(
                CapabilityRelationRequirement(
                    id=requirement_id,
                    capability_revision_id=revision_id,
                    knowledge_relation_revision_id=relation.id,
                    role="required" if spec.required else "supporting",
                    required=spec.required,
                    minimum_stage=spec.minimum_stage,
                    purpose=spec.purpose,
                    position=position,
                )
            )
        relation_payload.append(
            {
                "knowledgeRelationRevisionId": relation.id,
                "role": "required" if spec.required else "supporting",
                "required": spec.required,
                "minimumStage": spec.minimum_stage,
                "purpose": spec.purpose,
                "position": position,
            }
        )

    subnet_hash = _capability_subnet_hash(
        capability_revision_id=revision_id,
        knowledge_network_revision_id=frozen_network.revision.id,
        concepts=concept_payload,
        relations=relation_payload,
        boundary=boundary or {},
        context=context or {},
    )
    subnet_id = _stable_id("capability_subnet", revision_id)
    if db.get(CapabilitySubnet, subnet_id) is None:
        db.add(
            CapabilitySubnet(
                id=subnet_id,
                capability_revision_id=revision_id,
                knowledge_network_revision_id=frozen_network.revision.id,
                boundary_json=_dump(boundary or {}),
                context_json=_dump(context or {}),
                content_hash=subnet_hash,
                status="frozen",
            )
        )

    criterion_specs = (
        ("bronze", 1, f"能够辨认并说明{label}的核心对象与结论", "choice_quiz", "initial_or_novel", "unassisted", "taught", "choice_quiz_v1"),
        ("silver", 1, f"能够解释{label}的关键机制与必需关系", "oral_explanation", "independent", "unassisted_oral", "taught_with_variation", "oral_explanation_v1"),
        ("silver", 2, f"能够说明{label}的适用边界和常见混淆", "oral_boundary", "independent", "unassisted_oral", "boundary_or_confusion", "oral_boundary_v1"),
        ("gold", 1, f"能够在未见过的标准任务中运用{label}", "standard_application", "unseen", "unassisted", "standard_novel", "standard_application_v1"),
        ("diamond", 1, f"能够在陌生或综合情境中迁移运用{label}", "transfer_task", "unseen", "unassisted", "unfamiliar_or_integrated", "transfer_task_v1"),
    )
    criteria: dict[tuple[str, int], CapabilityStageCriterion] = {}
    for stage, position, statement, task_type, novelty, assistance, task_context, protocol in criterion_specs:
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
                context_requirement=task_context,
                required=True,
                verification_protocol=protocol,
            )
            db.add(criterion)
        criteria[(stage, position)] = criterion

    route_id = _stable_id("capability_route_binding", series_id, revision_id)
    if db.get(CapabilityRouteBinding, route_id) is None:
        db.add(
            CapabilityRouteBinding(
                id=route_id,
                series_id=series_id,
                capability_revision_id=revision_id,
                target_stage="bronze",
                route_json=_dump(
                    {
                        "naturalStageCeiling": natural_stage_ceiling,
                        "formalStageCeiling": "bronze",
                        "reason": "silver_requires_a_frozen_ask_me_contract",
                        "capabilitySubnetId": subnet_id,
                    }
                ),
                opportunities_json=_dump(
                    [
                        {
                            "stage": "bronze",
                            "criterionId": criteria[("bronze", 1)].id,
                            "verificationProtocol": "choice_quiz_v1",
                        }
                    ]
                ),
                status="active",
                rule_version=CAPABILITY_ROUTE_RULE_VERSION,
            )
        )
    db.flush()
    validate_capability_subnet(db, capability_revision_id=revision_id)
    return revision, criteria[("bronze", 1)]


def ensure_route_capability(
    db: Session,
    *,
    series_id: str,
    concept_revision_id: str,
) -> tuple[CapabilityRevision, CapabilityStageCriterion]:
    concept = db.get(ConceptRevision, concept_revision_id)
    if concept is None:
        raise AppError(
            "能力缺少有效的知识版本",
            code="CAPABILITY_CONCEPT_REVISION_MISSING",
            status=500,
        )
    return ensure_route_capability_subnet(
        db,
        series_id=series_id,
        label=f"理解并运用{concept.label}",
        concepts=(
            CapabilityConceptSpec(
                concept_revision_id=concept_revision_id,
                role="anchor",
                required=True,
            ),
        ),
    )


def bind_assessment_target_to_capability_subnet(
    db: Session,
    *,
    assessment_target_id: str,
    capability_revision_id: str,
    stage_criterion_id: str,
) -> None:
    """Freeze the exact subnet members one assessment stage may attribute."""

    validate_capability_subnet(db, capability_revision_id=capability_revision_id)
    target = db.get(AssessmentTarget, assessment_target_id)
    criterion = db.get(CapabilityStageCriterion, stage_criterion_id)
    if (
        target is None
        or criterion is None
        or criterion.capability_revision_id != capability_revision_id
        or target.capability_revision_id != capability_revision_id
        or target.capability_stage_criterion_id != stage_criterion_id
    ):
        raise AppError(
            "考核目标与能力阶段标准不一致",
            code="ASSESSMENT_TARGET_CAPABILITY_SCOPE_INVALID",
            status=500,
        )
    concept_rows = db.scalars(
        select(CapabilityConceptBinding)
        .where(
            CapabilityConceptBinding.capability_revision_id
            == capability_revision_id,
            CapabilityConceptBinding.role.in_(("anchor", "required")),
        )
        .order_by(CapabilityConceptBinding.position)
    ).all()
    for position, item in enumerate(concept_rows, start=1):
        binding_id = _stable_id(
            "assessment_target_concept", assessment_target_id, item.concept_revision_id
        )
        if db.get(AssessmentTargetConceptBinding, binding_id) is None:
            db.add(
                AssessmentTargetConceptBinding(
                    id=binding_id,
                    assessment_target_id=assessment_target_id,
                    concept_revision_id=item.concept_revision_id,
                    role=item.role,
                    required=item.required,
                    position=position,
                )
            )
    stage_order = {"bronze": 1, "silver": 2, "gold": 3, "diamond": 4}
    criterion_order = stage_order[criterion.stage]
    relation_rows = db.scalars(
        select(CapabilityRelationRequirement)
        .where(
            CapabilityRelationRequirement.capability_revision_id
            == capability_revision_id
        )
        .order_by(CapabilityRelationRequirement.position)
    ).all()
    included_relations = [
        item
        for item in relation_rows
        if stage_order[item.minimum_stage] <= criterion_order
    ]
    for position, item in enumerate(included_relations, start=1):
        binding_id = _stable_id(
            "assessment_target_relation",
            assessment_target_id,
            item.knowledge_relation_revision_id,
        )
        if db.get(AssessmentTargetRelationBinding, binding_id) is None:
            db.add(
                AssessmentTargetRelationBinding(
                    id=binding_id,
                    assessment_target_id=assessment_target_id,
                    knowledge_relation_revision_id=(
                        item.knowledge_relation_revision_id
                    ),
                    required=item.required,
                    position=position,
                )
            )
    db.flush()


def ensure_ask_me_stage_targets(
    db: Session,
    *,
    series_id: str,
    capability_revision_id: str,
    concept_revision_id: str,
) -> dict[str, AssessmentTarget]:
    """Materialize non-gate oral targets with exact stage-criterion bindings."""

    validate_capability_subnet(
        db, capability_revision_id=capability_revision_id
    )
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
            db.flush()
        bind_assessment_target_to_capability_subnet(
            db,
            assessment_target_id=target.id,
            capability_revision_id=capability_revision_id,
            stage_criterion_id=criterion.id,
        )
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

    validate_capability_subnet(
        db, capability_revision_id=capability_revision_id
    )
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
