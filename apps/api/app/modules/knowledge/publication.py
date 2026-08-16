from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Capability,
    CapabilityConceptBinding,
    CapabilityPlanningCandidate,
    CapabilityPlanningDecision,
    CapabilityRelationRequirement,
    CapabilityRevision,
    CapabilityStageCriterion,
    CapabilitySubnet,
    Concept,
    ConceptRevision,
    IdentityPublicationDecision,
    KnowledgeIdentityCandidate,
    KnowledgeIdentityDecision,
    KnowledgeRelation,
    KnowledgeRelationCandidate,
    KnowledgeRelationIdentityDecision,
    KnowledgeRelationRevision,
    PublishedCapabilityIdentity,
    PublishedConceptIdentity,
    PublishedRelationIdentity,
)
from ..curriculum.capability_planning import (
    capability_candidate_semantic_hash,
    relation_candidate_semantic_hash,
)
from ..learning.capabilities import (
    CAPABILITY_RUBRIC_VERSION,
    CAPABILITY_SUBNET_RULE_VERSION,
    CapabilityConceptSpec,
    CapabilityRelationSpec,
    _capability_subnet_hash,
    ensure_capability_route_binding,
    ensure_route_capability_subnet,
    validate_capability_subnet,
)
from .networks import (
    KNOWLEDGE_NETWORK_RULE_VERSION,
    KnowledgeRelationSpec,
    freeze_knowledge_network,
)


IDENTITY_PUBLICATION_RULE_VERSION = "cross_series_identity_publication_v1"


@dataclass(frozen=True)
class PublishedCapabilityResult:
    capability_revision_id: str
    concept_revision_ids: tuple[str, ...]
    relation_revision_ids: tuple[str, ...]
    decision: str


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _record_publication_decision(
    db: Session,
    *,
    subject_kind: str,
    candidate_id: str,
    decision: str,
    resolved_revision_id: str | None,
    compared_revision_ids: list[str],
    basis: dict,
    reviewer_id: str,
) -> IdentityPublicationDecision:
    previous = db.scalar(
        select(IdentityPublicationDecision)
        .where(
            IdentityPublicationDecision.subject_kind == subject_kind,
            IdentityPublicationDecision.candidate_id == candidate_id,
        )
        .order_by(IdentityPublicationDecision.created_at.desc())
    )
    payload = {
        "subjectKind": subject_kind,
        "candidateId": candidate_id,
        "decision": decision,
        "resolvedRevisionId": resolved_revision_id,
        "comparedRevisionIds": sorted(compared_revision_ids),
        "basis": basis,
        "reviewerId": reviewer_id,
        "ruleVersion": IDENTITY_PUBLICATION_RULE_VERSION,
        "supersedesId": previous.id if previous else None,
    }
    decision_hash = _hash(payload)
    existing = db.scalar(
        select(IdentityPublicationDecision).where(
            IdentityPublicationDecision.decision_hash == decision_hash
        )
    )
    if existing is not None:
        return existing
    row = IdentityPublicationDecision(
        id=_stable_id("identity_publication_decision", decision_hash),
        subject_kind=subject_kind,
        candidate_id=candidate_id,
        decision=decision,
        resolved_revision_id=resolved_revision_id,
        compared_revision_ids_json=_dump(sorted(compared_revision_ids)),
        basis_json=_dump(basis),
        actor_kind="reviewer",
        actor_id=reviewer_id,
        rule_version=IDENTITY_PUBLICATION_RULE_VERSION,
        supersedes_id=previous.id if previous else None,
        decision_hash=decision_hash,
    )
    db.add(row)
    db.flush()
    return row


def _concept_candidate_for_revision(
    db: Session, concept_revision_id: str
) -> KnowledgeIdentityCandidate:
    candidate = db.scalar(
        select(KnowledgeIdentityCandidate)
        .join(
            KnowledgeIdentityDecision,
            KnowledgeIdentityDecision.candidate_id
            == KnowledgeIdentityCandidate.id,
        )
        .where(
            KnowledgeIdentityDecision.resolved_concept_revision_id
            == concept_revision_id
        )
        .order_by(KnowledgeIdentityDecision.created_at.desc())
    )
    if candidate is None:
        raise AppError(
            "能力成员缺少可审核的知识候选来源",
            code="CONCEPT_PUBLICATION_CANDIDATE_MISSING",
            status=409,
        )
    return candidate


def _append_concept_resolution(
    db: Session,
    *,
    candidate: KnowledgeIdentityCandidate,
    revision_id: str,
    decision: str,
    compared_revision_ids: list[str],
    publication_id: str,
    reviewer_id: str,
) -> None:
    previous = db.scalar(
        select(KnowledgeIdentityDecision)
        .where(KnowledgeIdentityDecision.candidate_id == candidate.id)
        .order_by(KnowledgeIdentityDecision.created_at.desc())
    )
    payload = {
        "candidateId": candidate.id,
        "decision": decision,
        "resolvedConceptRevisionId": revision_id,
        "publicationId": publication_id,
        "reviewerId": reviewer_id,
        "supersedesId": previous.id if previous else None,
        "ruleVersion": IDENTITY_PUBLICATION_RULE_VERSION,
    }
    decision_hash = _hash(payload)
    if db.scalar(
        select(KnowledgeIdentityDecision).where(
            KnowledgeIdentityDecision.decision_hash == decision_hash
        )
    ) is None:
        db.add(
            KnowledgeIdentityDecision(
                id=_stable_id("knowledge_decision", decision_hash),
                candidate_id=candidate.id,
                decision=decision,
                resolved_concept_revision_id=revision_id,
                compared_revision_ids_json=_dump(compared_revision_ids),
                basis_json=_dump(
                    {
                        "mode": "reviewed_cross_series_publication",
                        "publicationId": publication_id,
                    }
                ),
                actor_kind="reviewer",
                actor_id=reviewer_id,
                rule_version=IDENTITY_PUBLICATION_RULE_VERSION,
                model_version="",
                supersedes_id=previous.id if previous else None,
                decision_hash=decision_hash,
            )
        )
    candidate.status = "resolved"
    db.flush()


def _publish_concept(
    db: Session,
    *,
    source_revision_id: str,
    reviewer_id: str,
    review: dict,
    supersedes_publication_id: str | None,
) -> PublishedConceptIdentity:
    candidate = _concept_candidate_for_revision(db, source_revision_id)
    exact = db.scalar(
        select(PublishedConceptIdentity).where(
            PublishedConceptIdentity.semantic_hash == candidate.candidate_hash,
            PublishedConceptIdentity.status == "published",
        )
    )
    family_rows = db.scalars(
        select(PublishedConceptIdentity).where(
            PublishedConceptIdentity.family_key == candidate.candidate_key,
            PublishedConceptIdentity.semantic_hash != candidate.candidate_hash,
            PublishedConceptIdentity.status == "published",
        )
    ).all()
    if exact is not None:
        _record_publication_decision(
            db,
            subject_kind="concept",
            candidate_id=candidate.id,
            decision="reuse_published",
            resolved_revision_id=exact.concept_revision_id,
            compared_revision_ids=[exact.concept_revision_id],
            basis={"publicationId": exact.id, "mode": "exact_semantic_hash"},
            reviewer_id=reviewer_id,
        )
        _append_concept_resolution(
            db,
            candidate=candidate,
            revision_id=exact.concept_revision_id,
            decision="reuse_published",
            compared_revision_ids=[exact.concept_revision_id],
            publication_id=exact.id,
            reviewer_id=reviewer_id,
        )
        return exact

    supersedes = (
        db.get(PublishedConceptIdentity, supersedes_publication_id)
        if supersedes_publication_id
        else None
    )
    if family_rows and (
        supersedes is None or supersedes.id not in {item.id for item in family_rows}
    ):
        _record_publication_decision(
            db,
            subject_kind="concept",
            candidate_id=candidate.id,
            decision="unresolved",
            resolved_revision_id=None,
            compared_revision_ids=[item.concept_revision_id for item in family_rows],
            basis={"mode": "published_family_semantic_conflict"},
            reviewer_id=reviewer_id,
        )
        candidate.status = "unresolved"
        db.flush()
        raise AppError(
            "知识候选与已发布家族语义冲突，需要明确新版本裁决",
            code="CONCEPT_PUBLICATION_UNRESOLVED",
            status=409,
        )

    source = db.get(ConceptRevision, source_revision_id)
    concept = db.get(Concept, source.concept_id) if source else None
    if source is None or concept is None:
        raise AppError(
            "知识候选版本不存在",
            code="CONCEPT_PUBLICATION_SOURCE_MISSING",
            status=409,
        )
    if supersedes is not None:
        previous_revision = db.get(
            ConceptRevision, supersedes.concept_revision_id
        )
        if previous_revision is None:
            raise AppError(
                "被取代的已发布知识版本不存在",
                code="CONCEPT_PUBLICATION_SUPERSEDES_MISSING",
                status=409,
            )
        next_revision = (
            db.scalar(
                select(func.max(ConceptRevision.revision)).where(
                    ConceptRevision.concept_id == previous_revision.concept_id
                )
            )
            or 0
        ) + 1
        published_revision_id = _stable_id(
            "concept_revision_published",
            previous_revision.concept_id,
            candidate.candidate_hash,
        )
        published_revision = db.get(
            ConceptRevision, published_revision_id
        )
        if published_revision is None:
            published_revision = ConceptRevision(
                id=published_revision_id,
                concept_id=previous_revision.concept_id,
                revision=next_revision,
                label=source.label,
                definition=source.definition,
                scope_json=source.scope_json,
                boundaries_json=source.boundaries_json,
                provenance_mode="reviewed_cross_series_publication",
                verification_status="reviewed",
                supersedes_id=previous_revision.id,
            )
            db.add(published_revision)
            db.flush()
    else:
        source.verification_status = "reviewed"
        source.provenance_mode = "reviewed_cross_series_publication"
        concept.origin = "reviewed_cross_series_publication"
        published_revision = source
    publication = PublishedConceptIdentity(
        id=_stable_id("published_concept", candidate.candidate_hash),
        family_key=candidate.candidate_key,
        semantic_hash=candidate.candidate_hash,
        concept_revision_id=published_revision.id,
        status="published",
        supersedes_id=supersedes.id if supersedes else None,
        review_json=_dump(review),
    )
    db.add(publication)
    db.flush()
    decision = "publish_new_revision" if supersedes else "publish_new_identity"
    _record_publication_decision(
        db,
        subject_kind="concept",
        candidate_id=candidate.id,
        decision=decision,
        resolved_revision_id=published_revision.id,
        compared_revision_ids=(
            [supersedes.concept_revision_id] if supersedes else []
        ),
        basis={"publicationId": publication.id, "review": review},
        reviewer_id=reviewer_id,
    )
    _append_concept_resolution(
        db,
        candidate=candidate,
        revision_id=published_revision.id,
        decision=decision,
        compared_revision_ids=(
            [supersedes.concept_revision_id] if supersedes else []
        ),
        publication_id=publication.id,
        reviewer_id=reviewer_id,
    )
    return publication


def publish_concept_candidate(
    db: Session,
    *,
    candidate_id: str,
    reviewer_id: str,
    review: dict,
    supersedes_publication_id: str | None = None,
) -> PublishedConceptIdentity:
    """Resolve one previously unresolved concept as a reviewed published version."""

    candidate = db.get(KnowledgeIdentityCandidate, candidate_id)
    decision = (
        db.scalar(
            select(KnowledgeIdentityDecision)
            .where(KnowledgeIdentityDecision.candidate_id == candidate_id)
            .order_by(KnowledgeIdentityDecision.created_at.desc())
        )
        if candidate is not None
        else None
    )
    if (
        candidate is None
        or decision is None
        or not decision.resolved_concept_revision_id
    ):
        raise AppError(
            "知识候选缺少可发布版本",
            code="CONCEPT_PUBLICATION_SOURCE_MISSING",
            status=409,
        )
    if not reviewer_id.strip() or not isinstance(review, dict) or not review:
        raise AppError(
            "跨系列发布必须记录审核者和审核依据",
            code="IDENTITY_PUBLICATION_REVIEW_REQUIRED",
            status=409,
        )
    return _publish_concept(
        db,
        source_revision_id=decision.resolved_concept_revision_id,
        reviewer_id=reviewer_id,
        review=review,
        supersedes_publication_id=supersedes_publication_id,
    )


def _relation_candidate(
    db: Session,
    *,
    chapter_id: str,
    semantic_hash: str,
) -> KnowledgeRelationCandidate | None:
    return db.scalar(
        select(KnowledgeRelationCandidate).where(
            KnowledgeRelationCandidate.chapter_id == chapter_id,
            KnowledgeRelationCandidate.candidate_hash == semantic_hash,
        )
    )


def _append_relation_resolution(
    db: Session,
    *,
    candidate: KnowledgeRelationCandidate | None,
    revision_id: str,
    decision: str,
    publication_id: str,
    reviewer_id: str,
) -> None:
    if candidate is None:
        return
    previous = db.scalar(
        select(KnowledgeRelationIdentityDecision)
        .where(KnowledgeRelationIdentityDecision.candidate_id == candidate.id)
        .order_by(KnowledgeRelationIdentityDecision.created_at.desc())
    )
    payload = {
        "candidateId": candidate.id,
        "decision": decision,
        "revisionId": revision_id,
        "publicationId": publication_id,
        "reviewerId": reviewer_id,
        "supersedesId": previous.id if previous else None,
        "ruleVersion": IDENTITY_PUBLICATION_RULE_VERSION,
    }
    decision_hash = _hash(payload)
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
                resolved_relation_revision_id=revision_id,
                compared_revision_ids_json="[]",
                basis_json=_dump(
                    {
                        "mode": "reviewed_cross_series_publication",
                        "publicationId": publication_id,
                    }
                ),
                actor_kind="reviewer",
                actor_id=reviewer_id,
                rule_version=IDENTITY_PUBLICATION_RULE_VERSION,
                model_version="",
                supersedes_id=previous.id if previous else None,
                decision_hash=decision_hash,
            )
        )
    candidate.status = "resolved"
    db.flush()


def _record_capability_resolution(
    db: Session,
    *,
    candidate: CapabilityPlanningCandidate,
    revision_id: str,
    decision: str,
    publication_id: str,
    reviewer_id: str,
) -> None:
    previous = db.scalar(
        select(CapabilityPlanningDecision)
        .where(CapabilityPlanningDecision.candidate_id == candidate.id)
        .order_by(CapabilityPlanningDecision.created_at.desc())
    )
    payload = {
        "candidateId": candidate.id,
        "decision": decision,
        "revisionId": revision_id,
        "publicationId": publication_id,
        "reviewerId": reviewer_id,
        "supersedesId": previous.id if previous else None,
        "ruleVersion": IDENTITY_PUBLICATION_RULE_VERSION,
    }
    decision_hash = _hash(payload)
    if db.scalar(
        select(CapabilityPlanningDecision).where(
            CapabilityPlanningDecision.decision_hash == decision_hash
        )
    ) is None:
        db.add(
            CapabilityPlanningDecision(
                id=_stable_id("capability_planning_decision", decision_hash),
                candidate_id=candidate.id,
                decision=decision,
                resolved_capability_revision_id=revision_id,
                basis_json=_dump(
                    {
                        "mode": "reviewed_cross_series_publication",
                        "publicationId": publication_id,
                    }
                ),
                actor_kind="reviewer",
                actor_id=reviewer_id,
                rule_version=IDENTITY_PUBLICATION_RULE_VERSION,
                model_version="",
                supersedes_id=previous.id if previous else None,
                decision_hash=decision_hash,
            )
        )
    candidate.status = "resolved"
    db.flush()


def _create_published_capability_revision(
    db: Session,
    *,
    source_revision: CapabilityRevision,
    candidate: CapabilityPlanningCandidate,
    members: list[dict],
    relations: list[dict],
    relation_revision_ids: list[str],
    supersedes: PublishedCapabilityIdentity | None,
) -> CapabilityRevision:
    previous_revision = (
        db.get(CapabilityRevision, supersedes.capability_revision_id)
        if supersedes
        else None
    )
    if supersedes and previous_revision is None:
        raise AppError(
            "被取代的已发布能力版本不存在",
            code="CAPABILITY_PUBLICATION_SUPERSEDES_MISSING",
            status=409,
        )
    if previous_revision is not None:
        capability_id = previous_revision.capability_id
        revision_number = (
            db.scalar(
                select(func.max(CapabilityRevision.revision)).where(
                    CapabilityRevision.capability_id == capability_id
                )
            )
            or 0
        ) + 1
    else:
        capability_id = _stable_id(
            "capability_published_identity", candidate.candidate_key
        )
        revision_number = 1
        if db.get(Capability, capability_id) is None:
            db.add(
                Capability(
                    id=capability_id,
                    namespace="published_capability",
                    capability_key=candidate.candidate_key,
                    canonical_name=candidate.label,
                    status="active",
                    origin="reviewed_cross_series_publication",
                )
            )
            db.flush()

    published_network = freeze_knowledge_network(
        db,
        namespace="published_knowledge_network",
        label=candidate.label,
        concept_revision_ids=[item["conceptRevisionId"] for item in members],
        relations=[
            KnowledgeRelationSpec(
                from_concept_revision_id=item["fromConceptRevisionId"],
                to_concept_revision_id=item["toConceptRevisionId"],
                relation_type=item["relationType"],
                statement=item["statement"],
                scope=item.get("scope", {}),
                provenance={"mode": "reviewed_cross_series_publication"},
                reuse_relation_revision_id=relation_revision_id,
            )
            for item, relation_revision_id in zip(
                relations, relation_revision_ids, strict=True
            )
        ],
        boundary=json.loads(candidate.boundary_json or "{}"),
        status="published",
        provenance_mode="reviewed_cross_series_publication",
    )
    revision_id = _stable_id(
        "capability_revision_published",
        capability_id,
        candidate.candidate_hash,
        revision_number,
    )
    revision = db.get(CapabilityRevision, revision_id)
    if revision is None:
        revision = CapabilityRevision(
            id=revision_id,
            capability_id=capability_id,
            revision=revision_number,
            label=candidate.label,
            scope_json=_dump(
                {
                    "knowledgeNetworkRevisionId": published_network.revision.id,
                    "subnetRuleVersion": CAPABILITY_SUBNET_RULE_VERSION,
                    "rubricVersion": CAPABILITY_RUBRIC_VERSION,
                }
            ),
            operation_json=source_revision.operation_json,
            context_constraints_json=source_revision.context_constraints_json,
            natural_stage_ceiling=candidate.natural_stage_ceiling,
            provenance_mode="reviewed_cross_series_publication",
            verification_status="published",
            supersedes_id=previous_revision.id if previous_revision else None,
        )
        db.add(revision)
        db.flush()

    concept_payload = []
    for position, item in enumerate(members, start=1):
        binding_id = _stable_id(
            "capability_concept", revision.id, item["conceptRevisionId"]
        )
        if db.get(CapabilityConceptBinding, binding_id) is None:
            db.add(
                CapabilityConceptBinding(
                    id=binding_id,
                    capability_revision_id=revision.id,
                    concept_revision_id=item["conceptRevisionId"],
                    role=item["role"],
                    position=position,
                    required=item["required"],
                )
            )
        concept_payload.append(
            {
                "conceptRevisionId": item["conceptRevisionId"],
                "role": item["role"],
                "required": item["required"],
                "position": position,
            }
        )

    source_requirements = db.scalars(
        select(CapabilityRelationRequirement)
        .where(
            CapabilityRelationRequirement.capability_revision_id
            == source_revision.id
        )
        .order_by(CapabilityRelationRequirement.position)
    ).all()
    relation_payload = []
    for position, (source_requirement, relation_revision_id) in enumerate(
        zip(source_requirements, relation_revision_ids, strict=True), start=1
    ):
        requirement_id = _stable_id(
            "capability_relation_requirement",
            revision.id,
            relation_revision_id,
        )
        if db.get(CapabilityRelationRequirement, requirement_id) is None:
            db.add(
                CapabilityRelationRequirement(
                    id=requirement_id,
                    capability_revision_id=revision.id,
                    knowledge_relation_revision_id=relation_revision_id,
                    role=source_requirement.role,
                    required=source_requirement.required,
                    minimum_stage=source_requirement.minimum_stage,
                    purpose=source_requirement.purpose,
                    position=position,
                )
            )
        relation_payload.append(
            {
                "knowledgeRelationRevisionId": relation_revision_id,
                "role": source_requirement.role,
                "required": source_requirement.required,
                "minimumStage": source_requirement.minimum_stage,
                "purpose": source_requirement.purpose,
                "position": position,
            }
        )

    boundary = json.loads(candidate.boundary_json or "{}")
    context = json.loads(source_revision.context_constraints_json or "{}")
    subnet_hash = _capability_subnet_hash(
        capability_revision_id=revision.id,
        knowledge_network_revision_id=published_network.revision.id,
        concepts=concept_payload,
        relations=relation_payload,
        boundary=boundary,
        context=context,
    )
    subnet_id = _stable_id("capability_subnet", revision.id)
    if db.get(CapabilitySubnet, subnet_id) is None:
        db.add(
            CapabilitySubnet(
                id=subnet_id,
                capability_revision_id=revision.id,
                knowledge_network_revision_id=published_network.revision.id,
                boundary_json=_dump(boundary),
                context_json=_dump(context),
                content_hash=subnet_hash,
                status="frozen",
            )
        )

    source_criteria = db.scalars(
        select(CapabilityStageCriterion)
        .where(
            CapabilityStageCriterion.capability_revision_id
            == source_revision.id
        )
        .order_by(
            CapabilityStageCriterion.stage,
            CapabilityStageCriterion.position,
        )
    ).all()
    for source_criterion in source_criteria:
        criterion_id = _stable_id(
            "capability_criterion",
            revision.id,
            source_criterion.stage,
            source_criterion.position,
        )
        if db.get(CapabilityStageCriterion, criterion_id) is None:
            db.add(
                CapabilityStageCriterion(
                    id=criterion_id,
                    capability_revision_id=revision.id,
                    stage=source_criterion.stage,
                    position=source_criterion.position,
                    statement=source_criterion.statement,
                    task_type=source_criterion.task_type,
                    novelty_requirement=source_criterion.novelty_requirement,
                    assistance_limit=source_criterion.assistance_limit,
                    context_requirement=source_criterion.context_requirement,
                    required=source_criterion.required,
                    verification_protocol=source_criterion.verification_protocol,
                )
            )
    db.flush()
    validate_capability_subnet(db, capability_revision_id=revision.id)
    ensure_capability_route_binding(
        db,
        series_id=candidate.series_id,
        capability_revision_id=revision.id,
    )
    return revision


def publish_capability_candidate(
    db: Session,
    *,
    planning_candidate_id: str,
    reviewer_id: str,
    review: dict,
    concept_supersedes: dict[str, str] | None = None,
    relation_supersedes: dict[str, str] | None = None,
    capability_supersedes_publication_id: str | None = None,
) -> PublishedCapabilityResult:
    """Publish one reviewed capability subnet and make exact reuse cross-series."""

    if not reviewer_id.strip() or not isinstance(review, dict) or not review:
        raise AppError(
            "跨系列发布必须记录审核者和审核依据",
            code="IDENTITY_PUBLICATION_REVIEW_REQUIRED",
            status=409,
        )
    concept_supersedes = concept_supersedes or {}
    relation_supersedes = relation_supersedes or {}
    candidate = db.get(CapabilityPlanningCandidate, planning_candidate_id)
    if candidate is None:
        raise AppError(
            "能力规划候选不存在",
            code="CAPABILITY_PUBLICATION_CANDIDATE_MISSING",
            status=404,
        )
    members = json.loads(candidate.members_json or "[]")
    relations = json.loads(candidate.relations_json or "[]")
    if not members:
        raise AppError(
            "能力规划候选缺少知识成员",
            code="CAPABILITY_PUBLICATION_MEMBERS_MISSING",
            status=409,
        )

    concept_map: dict[str, str] = {}
    for member in members:
        source_id = str(member["conceptRevisionId"])
        source_candidate = _concept_candidate_for_revision(db, source_id)
        publication = _publish_concept(
            db,
            source_revision_id=source_id,
            reviewer_id=reviewer_id,
            review=review,
            supersedes_publication_id=concept_supersedes.get(
                source_candidate.candidate_key
            ),
        )
        concept_map[source_id] = publication.concept_revision_id

    published_members = [
        {
            **item,
            "conceptRevisionId": concept_map[str(item["conceptRevisionId"])],
        }
        for item in members
    ]
    published_relations = [
        {
            **item,
            "fromConceptRevisionId": concept_map[
                str(item["fromConceptRevisionId"])
            ],
            "toConceptRevisionId": concept_map[str(item["toConceptRevisionId"])],
        }
        for item in relations
    ]
    canonical_hash = capability_candidate_semantic_hash(
        candidate_key=candidate.candidate_key,
        label=candidate.label,
        operation=candidate.operation,
        boundary=json.loads(candidate.boundary_json or "{}").get(
            "description", ""
        ),
        members=published_members,
        relations=published_relations,
        natural_stage_ceiling=candidate.natural_stage_ceiling,
    )
    exact_capability = db.scalar(
        select(PublishedCapabilityIdentity).where(
            PublishedCapabilityIdentity.semantic_hash == canonical_hash,
            PublishedCapabilityIdentity.status == "published",
        )
    )
    if exact_capability is not None:
        ensure_capability_route_binding(
            db,
            series_id=candidate.series_id,
            capability_revision_id=exact_capability.capability_revision_id,
        )
        _record_publication_decision(
            db,
            subject_kind="capability",
            candidate_id=candidate.id,
            decision="reuse_published",
            resolved_revision_id=exact_capability.capability_revision_id,
            compared_revision_ids=[exact_capability.capability_revision_id],
            basis={"publicationId": exact_capability.id},
            reviewer_id=reviewer_id,
        )
        _record_capability_resolution(
            db,
            candidate=candidate,
            revision_id=exact_capability.capability_revision_id,
            decision="reuse_published",
            publication_id=exact_capability.id,
            reviewer_id=reviewer_id,
        )
        return PublishedCapabilityResult(
            capability_revision_id=exact_capability.capability_revision_id,
            concept_revision_ids=tuple(
                item["conceptRevisionId"] for item in published_members
            ),
            relation_revision_ids=(),
            decision="reuse_published",
        )

    capability_family = db.scalars(
        select(PublishedCapabilityIdentity).where(
            PublishedCapabilityIdentity.family_key == candidate.candidate_key,
            PublishedCapabilityIdentity.semantic_hash != canonical_hash,
            PublishedCapabilityIdentity.status == "published",
        )
    ).all()
    capability_supersedes = (
        db.get(
            PublishedCapabilityIdentity,
            capability_supersedes_publication_id,
        )
        if capability_supersedes_publication_id
        else None
    )
    if capability_family and (
        capability_supersedes is None
        or capability_supersedes.id not in {item.id for item in capability_family}
    ):
        _record_publication_decision(
            db,
            subject_kind="capability",
            candidate_id=candidate.id,
            decision="unresolved",
            resolved_revision_id=None,
            compared_revision_ids=[
                item.capability_revision_id for item in capability_family
            ],
            basis={"mode": "published_family_semantic_conflict"},
            reviewer_id=reviewer_id,
        )
        candidate.status = "unresolved"
        db.flush()
        raise AppError(
            "能力候选与已发布家族语义冲突，需要明确新版本裁决",
            code="CAPABILITY_PUBLICATION_UNRESOLVED",
            status=409,
        )

    relation_reuse: dict[str, PublishedRelationIdentity] = {}
    for item in published_relations:
        semantic_hash = relation_candidate_semantic_hash(item)
        family_key = (
            f"{item['fromConceptRevisionId']}:"
            f"{item['relationType']}:"
            f"{item['toConceptRevisionId']}"
        )
        exact = db.scalar(
            select(PublishedRelationIdentity).where(
                PublishedRelationIdentity.semantic_hash == semantic_hash,
                PublishedRelationIdentity.status == "published",
            )
        )
        conflicts = db.scalars(
            select(PublishedRelationIdentity).where(
                PublishedRelationIdentity.family_key == family_key,
                PublishedRelationIdentity.semantic_hash != semantic_hash,
                PublishedRelationIdentity.status == "published",
            )
        ).all()
        supersedes_id = relation_supersedes.get(family_key)
        supersedes = (
            db.get(PublishedRelationIdentity, supersedes_id)
            if supersedes_id
            else None
        )
        if exact is not None:
            relation_reuse[semantic_hash] = exact
        elif conflicts and (
            supersedes is None
            or supersedes.id not in {entry.id for entry in conflicts}
        ):
            source_candidate = _relation_candidate(
                db,
                chapter_id=candidate.chapter_id,
                semantic_hash=relation_candidate_semantic_hash(
                    {
                        **item,
                        "fromConceptRevisionId": next(
                            old
                            for old, new in concept_map.items()
                            if new == item["fromConceptRevisionId"]
                        ),
                        "toConceptRevisionId": next(
                            old
                            for old, new in concept_map.items()
                            if new == item["toConceptRevisionId"]
                        ),
                    }
                ),
            )
            _record_publication_decision(
                db,
                subject_kind="relation",
                candidate_id=source_candidate.id if source_candidate else candidate.id,
                decision="unresolved",
                resolved_revision_id=None,
                compared_revision_ids=[
                    entry.knowledge_relation_revision_id for entry in conflicts
                ],
                basis={"mode": "published_family_semantic_conflict"},
                reviewer_id=reviewer_id,
            )
            if source_candidate:
                source_candidate.status = "unresolved"
            db.flush()
            raise AppError(
                "知识关系与已发布家族语义冲突，需要明确新版本裁决",
                code="RELATION_PUBLICATION_UNRESOLVED",
                status=409,
            )

    relation_specs = []
    for item in published_relations:
        semantic_hash = relation_candidate_semantic_hash(item)
        exact = relation_reuse.get(semantic_hash)
        relation_specs.append(
            CapabilityRelationSpec(
                from_concept_revision_id=item["fromConceptRevisionId"],
                to_concept_revision_id=item["toConceptRevisionId"],
                relation_type=item["relationType"],
                statement=item["statement"],
                minimum_stage=item["minimumStage"],
                purpose=item["purpose"],
                required=item["required"],
                scope=item.get("scope", {}),
                provenance={"mode": "reviewed_cross_series_publication"},
                reuse_relation_revision_id=(
                    exact.knowledge_relation_revision_id if exact else None
                ),
            )
        )
    revision, _bronze = ensure_route_capability_subnet(
        db,
        series_id=candidate.series_id,
        label=candidate.label,
        concepts=[
            CapabilityConceptSpec(
                concept_revision_id=item["conceptRevisionId"],
                role=item["role"],
                required=item["required"],
            )
            for item in published_members
        ],
        relations=relation_specs,
        boundary=json.loads(candidate.boundary_json or "{}"),
        context={
            "operation": candidate.operation,
            "candidateKey": candidate.candidate_key,
        },
        natural_stage_ceiling=candidate.natural_stage_ceiling,
    )
    requirement_rows = db.scalars(
        select(CapabilityRelationRequirement)
        .where(
            CapabilityRelationRequirement.capability_revision_id == revision.id
        )
        .order_by(CapabilityRelationRequirement.position)
    ).all()
    published_relation_ids: list[str] = []
    for item, requirement in zip(
        published_relations, requirement_rows, strict=True
    ):
        semantic_hash = relation_candidate_semantic_hash(item)
        family_key = (
            f"{item['fromConceptRevisionId']}:"
            f"{item['relationType']}:"
            f"{item['toConceptRevisionId']}"
        )
        publication = relation_reuse.get(semantic_hash)
        if publication is None:
            relation_revision = db.get(
                KnowledgeRelationRevision,
                requirement.knowledge_relation_revision_id,
            )
            relation_identity = (
                db.get(KnowledgeRelation, relation_revision.knowledge_relation_id)
                if relation_revision
                else None
            )
            if relation_revision is None or relation_identity is None:
                raise AppError(
                    "能力发布生成的关系版本不存在",
                    code="RELATION_PUBLICATION_SOURCE_MISSING",
                    status=500,
                )
            supersedes_id = relation_supersedes.get(family_key)
            supersedes = (
                db.get(PublishedRelationIdentity, supersedes_id)
                if supersedes_id
                else None
            )
            if supersedes is not None:
                previous_revision = db.get(
                    KnowledgeRelationRevision,
                    supersedes.knowledge_relation_revision_id,
                )
                previous_identity = (
                    db.get(
                        KnowledgeRelation,
                        previous_revision.knowledge_relation_id,
                    )
                    if previous_revision
                    else None
                )
                if previous_revision is None or previous_identity is None:
                    raise AppError(
                        "被取代的已发布关系版本不存在",
                        code="RELATION_PUBLICATION_SUPERSEDES_MISSING",
                        status=409,
                    )
                next_revision = (
                    db.scalar(
                        select(func.max(KnowledgeRelationRevision.revision)).where(
                            KnowledgeRelationRevision.knowledge_relation_id
                            == previous_identity.id
                        )
                    )
                    or 0
                ) + 1
                provenance = {"mode": "reviewed_cross_series_publication"}
                relation_hash = _hash(
                    {
                        "ruleVersion": KNOWLEDGE_NETWORK_RULE_VERSION,
                        "namespace": previous_identity.namespace,
                        "fromConceptRevisionId": item[
                            "fromConceptRevisionId"
                        ],
                        "toConceptRevisionId": item["toConceptRevisionId"],
                        "relationType": item["relationType"],
                        "statement": " ".join(item["statement"].split()),
                        "scope": item.get("scope", {}),
                        "provenance": provenance,
                    }
                )
                published_relation_id = _stable_id(
                    "knowledge_relation_revision_published",
                    previous_identity.id,
                    semantic_hash,
                )
                published_relation = db.get(
                    KnowledgeRelationRevision, published_relation_id
                )
                if published_relation is None:
                    published_relation = KnowledgeRelationRevision(
                        id=published_relation_id,
                        knowledge_relation_id=previous_identity.id,
                        revision=next_revision,
                        from_concept_revision_id=item[
                            "fromConceptRevisionId"
                        ],
                        to_concept_revision_id=item["toConceptRevisionId"],
                        relation_type=item["relationType"],
                        statement=" ".join(item["statement"].split()),
                        scope_json=_dump(item.get("scope", {})),
                        provenance_json=_dump(provenance),
                        verification_status="published",
                        supersedes_id=previous_revision.id,
                        content_hash=relation_hash,
                    )
                    db.add(published_relation)
                    db.flush()
                relation_revision = published_relation
            else:
                relation_revision.verification_status = "published"
                relation_identity.origin = "reviewed_cross_series_publication"
            publication = PublishedRelationIdentity(
                id=_stable_id("published_relation", semantic_hash),
                family_key=family_key,
                semantic_hash=semantic_hash,
                knowledge_relation_revision_id=relation_revision.id,
                status="published",
                supersedes_id=supersedes.id if supersedes else None,
                review_json=_dump(review),
            )
            db.add(publication)
            db.flush()
        published_relation_ids.append(
            publication.knowledge_relation_revision_id
        )
        source_semantic_hash = relation_candidate_semantic_hash(
            {
                **item,
                "fromConceptRevisionId": next(
                    old
                    for old, new in concept_map.items()
                    if new == item["fromConceptRevisionId"]
                ),
                "toConceptRevisionId": next(
                    old
                    for old, new in concept_map.items()
                    if new == item["toConceptRevisionId"]
                ),
            }
        )
        source_candidate = _relation_candidate(
            db,
            chapter_id=candidate.chapter_id,
            semantic_hash=source_semantic_hash,
        )
        relation_decision = (
            "reuse_published"
            if semantic_hash in relation_reuse
            else "publish_new_revision"
            if publication.supersedes_id
            else "publish_new_identity"
        )
        _record_publication_decision(
            db,
            subject_kind="relation",
            candidate_id=(source_candidate.id if source_candidate else candidate.id),
            decision=relation_decision,
            resolved_revision_id=publication.knowledge_relation_revision_id,
            compared_revision_ids=(
                [
                    db.get(
                        PublishedRelationIdentity,
                        publication.supersedes_id,
                    ).knowledge_relation_revision_id
                ]
                if publication.supersedes_id
                else []
            ),
            basis={"publicationId": publication.id, "review": review},
            reviewer_id=reviewer_id,
        )
        _append_relation_resolution(
            db,
            candidate=source_candidate,
            revision_id=publication.knowledge_relation_revision_id,
            decision=relation_decision,
            publication_id=publication.id,
            reviewer_id=reviewer_id,
        )

    published_revision = _create_published_capability_revision(
        db,
        source_revision=revision,
        candidate=candidate,
        members=published_members,
        relations=published_relations,
        relation_revision_ids=published_relation_ids,
        supersedes=capability_supersedes,
    )
    publication = PublishedCapabilityIdentity(
        id=_stable_id("published_capability", canonical_hash),
        family_key=candidate.candidate_key,
        semantic_hash=canonical_hash,
        capability_revision_id=published_revision.id,
        status="published",
        supersedes_id=(
            capability_supersedes.id if capability_supersedes else None
        ),
        review_json=_dump(review),
    )
    db.add(publication)
    db.flush()
    decision = (
        "publish_new_revision"
        if capability_supersedes
        else "publish_new_identity"
    )
    _record_publication_decision(
        db,
        subject_kind="capability",
        candidate_id=candidate.id,
        decision=decision,
        resolved_revision_id=published_revision.id,
        compared_revision_ids=(
            [capability_supersedes.capability_revision_id]
            if capability_supersedes
            else []
        ),
        basis={"publicationId": publication.id, "review": review},
        reviewer_id=reviewer_id,
    )
    _record_capability_resolution(
        db,
        candidate=candidate,
        revision_id=published_revision.id,
        decision=decision,
        publication_id=publication.id,
        reviewer_id=reviewer_id,
    )
    db.flush()
    return PublishedCapabilityResult(
        capability_revision_id=published_revision.id,
        concept_revision_ids=tuple(
            item["conceptRevisionId"] for item in published_members
        ),
        relation_revision_ids=tuple(published_relation_ids),
        decision=decision,
    )
