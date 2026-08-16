from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    CapabilityPlanningCandidate,
    CapabilityPlanningDecision,
    CapabilityRelationRequirement,
    ConceptRevision,
    KnowledgeIdentityCandidate,
    KnowledgeRelationCandidate,
    KnowledgeRelationIdentityDecision,
    Section,
)
from ..knowledge.identity import candidate_semantic_hash, resolve_candidate_revision
from ..learning.capabilities import (
    CapabilityConceptSpec,
    CapabilityRelationSpec,
    ensure_route_capability_subnet,
)


CAPABILITY_PLANNING_RULE_VERSION = "chapter_capability_planning_v1"
RELATION_IDENTITY_RULE_VERSION = "knowledge_relation_exact_v1"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _resolve_section_concepts(
    db: Session,
    *,
    series_id: str,
    sections: list[Section],
    generated_sections: list,
    published_allowlist: list[dict],
) -> dict[int, ConceptRevision]:
    published_by_key = {
        str(item["conceptKey"]): str(item["conceptRevisionId"])
        for item in published_allowlist
    }
    result: dict[int, ConceptRevision] = {}
    for position, (section, generated) in enumerate(
        zip(sections, generated_sections, strict=True), start=1
    ):
        if generated.concept_candidate is not None:
            candidate = generated.concept_candidate.model_dump()
            revision = resolve_candidate_revision(
                db,
                series_id=series_id,
                section_id=section.id,
                candidate=candidate,
            )
            occurrence = db.scalar(
                select(KnowledgeIdentityCandidate).where(
                    KnowledgeIdentityCandidate.section_id == section.id,
                    KnowledgeIdentityCandidate.candidate_hash
                    == candidate_semantic_hash(candidate),
                )
            )
            if occurrence is None or occurrence.status != "resolved":
                raise AppError(
                    "综合能力引用的知识身份尚未解决",
                    code="CAPABILITY_PLAN_CONCEPT_UNRESOLVED",
                    status=409,
                )
            result[position] = revision
            continue
        concept_key = str(generated.baseline_concept_key or "").strip()
        revision_id = published_by_key.get(concept_key)
        revision = db.get(ConceptRevision, revision_id) if revision_id else None
        if revision is None:
            raise AppError(
                "综合能力引用的小节缺少稳定知识身份",
                code="CAPABILITY_PLAN_SECTION_CONCEPT_MISSING",
                status=409,
            )
        result[position] = revision
    return result


def _record_relation_decisions(
    db: Session,
    *,
    series_id: str,
    chapter_id: str,
    relation_payloads: list[dict],
    relation_requirements: list[CapabilityRelationRequirement],
) -> None:
    for payload, requirement in zip(
        relation_payloads, relation_requirements, strict=True
    ):
        semantic = {
            "fromConceptRevisionId": payload["fromConceptRevisionId"],
            "toConceptRevisionId": payload["toConceptRevisionId"],
            "relationType": payload["relationType"],
            "statement": payload["statement"],
            "scope": payload.get("scope", {}),
        }
        relation_family_key = (
            f"{semantic['fromConceptRevisionId']}:"
            f"{semantic['relationType']}:"
            f"{semantic['toConceptRevisionId']}"
        )
        candidate_hash = _hash(semantic)
        candidate_id = _stable_id(
            "knowledge_relation_candidate", chapter_id, candidate_hash
        )
        candidate = db.get(KnowledgeRelationCandidate, candidate_id)
        exact_prior = db.scalar(
            select(KnowledgeRelationIdentityDecision)
            .join(
                KnowledgeRelationCandidate,
                KnowledgeRelationCandidate.id
                == KnowledgeRelationIdentityDecision.candidate_id,
            )
            .where(
                KnowledgeRelationCandidate.series_id == series_id,
                KnowledgeRelationCandidate.candidate_hash == candidate_hash,
                KnowledgeRelationCandidate.chapter_id != chapter_id,
            )
            .limit(1)
        )
        semantic_conflict = db.scalar(
            select(KnowledgeRelationCandidate)
            .where(
                KnowledgeRelationCandidate.series_id == series_id,
                KnowledgeRelationCandidate.candidate_key
                == relation_family_key,
                KnowledgeRelationCandidate.candidate_hash != candidate_hash,
            )
            .limit(1)
        )
        if semantic_conflict is not None:
            raise AppError(
                "同一关系候选键出现不同语义，不能静默创建新关系",
                code="CAPABILITY_PLAN_RELATION_UNRESOLVED",
                status=409,
            )
        if candidate is None:
            candidate = KnowledgeRelationCandidate(
                id=candidate_id,
                series_id=series_id,
                chapter_id=chapter_id,
                candidate_key=relation_family_key,
                from_concept_revision_id=semantic["fromConceptRevisionId"],
                to_concept_revision_id=semantic["toConceptRevisionId"],
                relation_type=semantic["relationType"],
                statement=semantic["statement"],
                scope_json=_dump(semantic["scope"]),
                candidate_hash=candidate_hash,
                status="proposed",
                provenance_mode="chapter_capability_plan",
            )
            db.add(candidate)
            db.flush()
        decision = "reuse_revision" if exact_prior else "create_relation"
        decision_hash = _hash(
            {
                "candidateId": candidate.id,
                "decision": decision,
                "resolvedRelationRevisionId": (
                    requirement.knowledge_relation_revision_id
                ),
                "ruleVersion": RELATION_IDENTITY_RULE_VERSION,
            }
        )
        if db.scalar(
            select(KnowledgeRelationIdentityDecision).where(
                KnowledgeRelationIdentityDecision.decision_hash == decision_hash
            )
        ) is None:
            db.add(
                KnowledgeRelationIdentityDecision(
                    id=_stable_id("knowledge_relation_decision", decision_hash),
                    candidate_id=candidate.id,
                    decision=decision,
                    resolved_relation_revision_id=(
                        requirement.knowledge_relation_revision_id
                    ),
                    compared_revision_ids_json=_dump(
                        [exact_prior.resolved_relation_revision_id]
                        if exact_prior
                        and exact_prior.resolved_relation_revision_id
                        else []
                    ),
                    basis_json=_dump(
                        {
                            "mode": (
                                "exact_semantic_hash"
                                if exact_prior
                                else "new_route_relation"
                            )
                        }
                    ),
                    actor_kind="deterministic_rule",
                    actor_id="",
                    rule_version=RELATION_IDENTITY_RULE_VERSION,
                    model_version="",
                    supersedes_id=None,
                    decision_hash=decision_hash,
                )
            )
        candidate.status = "resolved"
    db.flush()


def freeze_chapter_capability_plans(
    db: Session,
    *,
    series_id: str,
    chapter_id: str,
    sections: list[Section],
    generated_chapter,
    published_allowlist: list[dict],
) -> None:
    """Resolve chapter candidates and attach frozen capability refs to objectives."""

    candidates = list(generated_chapter.capability_subnets)
    if not candidates:
        return
    concepts_by_position = _resolve_section_concepts(
        db,
        series_id=series_id,
        sections=sections,
        generated_sections=list(generated_chapter.sections),
        published_allowlist=published_allowlist,
    )
    for generated in candidates:
        concept_specs = [
            CapabilityConceptSpec(
                concept_revision_id=concepts_by_position[
                    item.section_position
                ].id,
                role=item.role,
                required=item.required,
            )
            for item in generated.members
        ]
        relation_specs = [
            CapabilityRelationSpec(
                from_concept_revision_id=concepts_by_position[
                    item.from_section_position
                ].id,
                to_concept_revision_id=concepts_by_position[
                    item.to_section_position
                ].id,
                relation_type=item.relation_type,
                statement=" ".join(item.statement.split()),
                minimum_stage=item.minimum_stage,
                purpose=item.purpose,
                required=item.required,
                scope={},
                provenance={"mode": "chapter_capability_plan"},
            )
            for item in generated.relations
        ]
        target_section = sections[generated.assessment_section_position - 1]
        member_payload = [
            {
                "sectionId": sections[item.section_position - 1].id,
                "sectionPosition": item.section_position,
                "conceptRevisionId": concepts_by_position[item.section_position].id,
                "role": item.role,
                "required": item.required,
            }
            for item in generated.members
        ]
        relation_payload = [
            {
                "fromConceptRevisionId": item.from_concept_revision_id,
                "toConceptRevisionId": item.to_concept_revision_id,
                "relationType": item.relation_type,
                "statement": item.statement,
                "minimumStage": item.minimum_stage,
                "purpose": item.purpose,
                "required": item.required,
                "scope": item.scope or {},
            }
            for item in relation_specs
        ]
        planning_payload = {
            "ruleVersion": CAPABILITY_PLANNING_RULE_VERSION,
            "candidateKey": generated.candidate_key,
            "label": generated.label,
            "operation": generated.operation,
            "boundary": generated.boundary,
            "members": member_payload,
            "relations": relation_payload,
            "assessmentSectionId": target_section.id,
            "assessmentObjectivePosition": (
                generated.assessment_objective_position
            ),
            "naturalStageCeiling": generated.natural_stage_ceiling,
        }
        semantic_payload = {
            key: value
            for key, value in planning_payload.items()
            if key
            not in {
                "assessmentSectionId",
                "assessmentObjectivePosition",
            }
        }
        semantic_payload["members"] = [
            {
                "conceptRevisionId": item["conceptRevisionId"],
                "role": item["role"],
                "required": item["required"],
            }
            for item in member_payload
        ]
        candidate_hash = _hash(semantic_payload)
        planning_candidate_id = _stable_id(
            "capability_planning_candidate", chapter_id, candidate_hash
        )
        planning_candidate = db.get(
            CapabilityPlanningCandidate, planning_candidate_id
        )
        if planning_candidate is None:
            planning_candidate = CapabilityPlanningCandidate(
                id=planning_candidate_id,
                series_id=series_id,
                chapter_id=chapter_id,
                candidate_key=generated.candidate_key,
                label=generated.label,
                operation=generated.operation,
                boundary_json=_dump({"description": generated.boundary}),
                members_json=_dump(member_payload),
                relations_json=_dump(relation_payload),
                assessment_section_id=target_section.id,
                assessment_objective_position=(
                    generated.assessment_objective_position
                ),
                natural_stage_ceiling=generated.natural_stage_ceiling,
                candidate_hash=candidate_hash,
                status="proposed",
                provenance_mode="chapter_outline",
            )
            db.add(planning_candidate)
            db.flush()

        for relation_item in relation_payload:
            relation_hash = _hash(
                {
                    "fromConceptRevisionId": relation_item[
                        "fromConceptRevisionId"
                    ],
                    "toConceptRevisionId": relation_item["toConceptRevisionId"],
                    "relationType": relation_item["relationType"],
                    "statement": relation_item["statement"],
                    "scope": relation_item.get("scope", {}),
                }
            )
            relation_family_key = (
                f"{relation_item['fromConceptRevisionId']}:"
                f"{relation_item['relationType']}:"
                f"{relation_item['toConceptRevisionId']}"
            )
            relation_conflict = db.scalar(
                select(KnowledgeRelationCandidate)
                .where(
                    KnowledgeRelationCandidate.series_id == series_id,
                    KnowledgeRelationCandidate.candidate_key
                    == relation_family_key,
                    KnowledgeRelationCandidate.candidate_hash != relation_hash,
                )
                .limit(1)
            )
            if relation_conflict is not None:
                planning_candidate.status = "unresolved"
                unresolved_candidate_id = _stable_id(
                    "knowledge_relation_candidate", chapter_id, relation_hash
                )
                unresolved_candidate = db.get(
                    KnowledgeRelationCandidate, unresolved_candidate_id
                )
                if unresolved_candidate is None:
                    unresolved_candidate = KnowledgeRelationCandidate(
                        id=unresolved_candidate_id,
                        series_id=series_id,
                        chapter_id=chapter_id,
                        candidate_key=relation_family_key,
                        from_concept_revision_id=relation_item[
                            "fromConceptRevisionId"
                        ],
                        to_concept_revision_id=relation_item[
                            "toConceptRevisionId"
                        ],
                        relation_type=relation_item["relationType"],
                        statement=relation_item["statement"],
                        scope_json=_dump(relation_item.get("scope", {})),
                        candidate_hash=relation_hash,
                        status="unresolved",
                        provenance_mode="chapter_capability_plan",
                    )
                    db.add(unresolved_candidate)
                    db.flush()
                conflict_decision_hash = _hash(
                    {
                        "candidateId": unresolved_candidate.id,
                        "decision": "unresolved",
                        "comparedCandidateId": relation_conflict.id,
                        "ruleVersion": RELATION_IDENTITY_RULE_VERSION,
                    }
                )
                if db.scalar(
                    select(KnowledgeRelationIdentityDecision).where(
                        KnowledgeRelationIdentityDecision.decision_hash
                        == conflict_decision_hash
                    )
                ) is None:
                    prior_decision = db.scalar(
                        select(KnowledgeRelationIdentityDecision)
                        .where(
                            KnowledgeRelationIdentityDecision.candidate_id
                            == relation_conflict.id
                        )
                        .order_by(
                            KnowledgeRelationIdentityDecision.created_at.desc()
                        )
                    )
                    db.add(
                        KnowledgeRelationIdentityDecision(
                            id=_stable_id(
                                "knowledge_relation_decision",
                                conflict_decision_hash,
                            ),
                            candidate_id=unresolved_candidate.id,
                            decision="unresolved",
                            resolved_relation_revision_id=None,
                            compared_revision_ids_json=_dump(
                                [prior_decision.resolved_relation_revision_id]
                                if prior_decision
                                and prior_decision.resolved_relation_revision_id
                                else []
                            ),
                            basis_json=_dump(
                                {
                                    "mode": "same_relation_family_semantic_conflict",
                                    "comparedCandidateId": relation_conflict.id,
                                }
                            ),
                            actor_kind="deterministic_rule",
                            actor_id="",
                            rule_version=RELATION_IDENTITY_RULE_VERSION,
                            model_version="",
                            supersedes_id=None,
                            decision_hash=conflict_decision_hash,
                        )
                    )
                db.flush()
                raise AppError(
                    "同一关系候选键出现不同语义，不能静默创建新关系",
                    code="CAPABILITY_PLAN_RELATION_UNRESOLVED",
                    status=409,
                )

        semantic_conflict = db.scalar(
            select(CapabilityPlanningCandidate)
            .where(
                CapabilityPlanningCandidate.series_id == series_id,
                CapabilityPlanningCandidate.candidate_key
                == generated.candidate_key,
                CapabilityPlanningCandidate.candidate_hash != candidate_hash,
            )
            .limit(1)
        )
        if semantic_conflict is not None:
            planning_candidate.status = "unresolved"
            conflict_hash = _hash(
                {
                    "candidateId": planning_candidate.id,
                    "decision": "unresolved_capability",
                    "comparedCandidateId": semantic_conflict.id,
                    "ruleVersion": CAPABILITY_PLANNING_RULE_VERSION,
                }
            )
            if db.scalar(
                select(CapabilityPlanningDecision).where(
                    CapabilityPlanningDecision.decision_hash == conflict_hash
                )
            ) is None:
                db.add(
                    CapabilityPlanningDecision(
                        id=_stable_id(
                            "capability_planning_decision", conflict_hash
                        ),
                        candidate_id=planning_candidate.id,
                        decision="unresolved_capability",
                        resolved_capability_revision_id=None,
                        basis_json=_dump(
                            {
                                "mode": "same_candidate_key_semantic_conflict",
                                "comparedCandidateId": semantic_conflict.id,
                            }
                        ),
                        actor_kind="deterministic_rule",
                        actor_id="",
                        rule_version=CAPABILITY_PLANNING_RULE_VERSION,
                        model_version="",
                        supersedes_id=None,
                        decision_hash=conflict_hash,
                    )
                )
            db.flush()
            raise AppError(
                "同一能力候选键出现不同子网语义，不能静默创建新能力",
                code="CAPABILITY_PLAN_IDENTITY_UNRESOLVED",
                status=409,
            )

        capability, bronze = ensure_route_capability_subnet(
            db,
            series_id=series_id,
            label=generated.label,
            concepts=concept_specs,
            relations=relation_specs,
            boundary={"description": generated.boundary},
            context={
                "operation": generated.operation,
                "candidateKey": generated.candidate_key,
            },
            natural_stage_ceiling=generated.natural_stage_ceiling,
        )
        prior = db.scalar(
            select(CapabilityPlanningDecision).where(
                CapabilityPlanningDecision.resolved_capability_revision_id
                == capability.id,
                CapabilityPlanningDecision.candidate_id != planning_candidate.id,
            )
        )
        decision = "reuse_route_capability" if prior else "create_capability"
        decision_hash = _hash(
            {
                "candidateId": planning_candidate.id,
                "decision": decision,
                "resolvedCapabilityRevisionId": capability.id,
                "ruleVersion": CAPABILITY_PLANNING_RULE_VERSION,
            }
        )
        if db.scalar(
            select(CapabilityPlanningDecision).where(
                CapabilityPlanningDecision.decision_hash == decision_hash
            )
        ) is None:
            db.add(
                CapabilityPlanningDecision(
                    id=_stable_id("capability_planning_decision", decision_hash),
                    candidate_id=planning_candidate.id,
                    decision=decision,
                    resolved_capability_revision_id=capability.id,
                    basis_json=_dump(
                        {
                            "mode": (
                                "exact_route_capability"
                                if prior
                                else "validated_chapter_subnet"
                            )
                        }
                    ),
                    actor_kind="deterministic_rule",
                    actor_id="",
                    rule_version=CAPABILITY_PLANNING_RULE_VERSION,
                    model_version="",
                    supersedes_id=None,
                    decision_hash=decision_hash,
                )
            )
        planning_candidate.status = "resolved"

        requirements = db.scalars(
            select(CapabilityRelationRequirement)
            .where(
                CapabilityRelationRequirement.capability_revision_id
                == capability.id
            )
            .order_by(CapabilityRelationRequirement.position)
        ).all()
        _record_relation_decisions(
            db,
            series_id=series_id,
            chapter_id=chapter_id,
            relation_payloads=relation_payload,
            relation_requirements=list(requirements),
        )

        anchor = next(
            item for item in concept_specs if item.role == "anchor"
        )
        objectives = json.loads(target_section.objectives_json or "[]")
        objective_index = generated.assessment_objective_position - 1
        if objective_index >= len(objectives) or not isinstance(
            objectives[objective_index], dict
        ):
            raise AppError(
                "综合能力验证位置没有结构化目标",
                code="CAPABILITY_PLAN_ASSESSMENT_TARGET_INVALID",
                status=409,
            )
        objectives[objective_index]["plannedCapability"] = {
            "capabilityRevisionId": capability.id,
            "stageCriterionId": bronze.id,
            "anchorConceptRevisionId": anchor.concept_revision_id,
            "planningCandidateId": planning_candidate.id,
            "ruleVersion": CAPABILITY_PLANNING_RULE_VERSION,
        }
        target_section.objectives_json = _dump(objectives)
    db.flush()
