from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ConceptRevision,
    KnowledgeGraphRelease,
    KnowledgeNetwork,
    KnowledgeNetworkConceptBinding,
    KnowledgeNetworkRelationBinding,
    KnowledgeNetworkRevision,
    KnowledgeRelation,
    KnowledgeRelationRevision,
)


KNOWLEDGE_NETWORK_RULE_VERSION = "knowledge_network_v1"
NETWORK_STATUSES = {"route_scoped", "reviewed", "published"}
RELATION_TYPES = {
    "applies_to",
    "causes",
    "contrasts_with",
    "contributes_to",
    "enables",
    "explains",
    "helps_explain",
    "part_of",
    "precedes",
    "prerequisite_for",
    "refines",
}


@dataclass(frozen=True)
class KnowledgeRelationSpec:
    from_concept_revision_id: str
    to_concept_revision_id: str
    relation_type: str
    statement: str
    scope: dict | None = None
    provenance: dict | None = None
    reuse_relation_revision_id: str | None = None


@dataclass(frozen=True)
class FrozenKnowledgeNetwork:
    revision: KnowledgeNetworkRevision
    relation_revisions: tuple[KnowledgeRelationRevision, ...]


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode()
    ).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _normalized_relation_payload(spec: KnowledgeRelationSpec) -> dict:
    return {
        "fromConceptRevisionId": spec.from_concept_revision_id,
        "toConceptRevisionId": spec.to_concept_revision_id,
        "relationType": spec.relation_type.strip(),
        "statement": " ".join(spec.statement.split()),
        "scope": spec.scope or {},
        "provenance": spec.provenance or {},
    }


def _validate_inputs(
    db: Session,
    *,
    concept_revision_ids: tuple[str, ...],
    relations: tuple[KnowledgeRelationSpec, ...],
    status: str,
    source_release_ids: tuple[str, ...],
) -> None:
    if status not in NETWORK_STATUSES:
        raise AppError(
            "知识网络状态无效",
            code="KNOWLEDGE_NETWORK_STATUS_INVALID",
            status=500,
        )
    if not concept_revision_ids or len(set(concept_revision_ids)) != len(
        concept_revision_ids
    ):
        raise AppError(
            "知识网络必须包含非重复的知识版本",
            code="KNOWLEDGE_NETWORK_CONCEPTS_INVALID",
            status=500,
        )
    concepts = db.scalars(
        select(ConceptRevision).where(ConceptRevision.id.in_(concept_revision_ids))
    ).all()
    if len(concepts) != len(concept_revision_ids):
        raise AppError(
            "知识网络引用了不存在的知识版本",
            code="KNOWLEDGE_NETWORK_CONCEPT_MISSING",
            status=500,
        )
    concept_ids = set(concept_revision_ids)
    relation_keys: set[tuple[str, str, str]] = set()
    for item in relations:
        relation_type = item.relation_type.strip()
        if relation_type not in RELATION_TYPES:
            raise AppError(
                "知识关系类型不受支持",
                code="KNOWLEDGE_NETWORK_RELATION_TYPE_INVALID",
                status=500,
            )
        if (
            item.from_concept_revision_id not in concept_ids
            or item.to_concept_revision_id not in concept_ids
            or item.from_concept_revision_id == item.to_concept_revision_id
        ):
            raise AppError(
                "知识关系端点必须属于同一网络且不能自连",
                code="KNOWLEDGE_NETWORK_RELATION_ENDPOINT_INVALID",
                status=500,
            )
        if not " ".join(item.statement.split()):
            raise AppError(
                "知识关系必须说明语义",
                code="KNOWLEDGE_NETWORK_RELATION_STATEMENT_MISSING",
                status=500,
            )
        key = (
            item.from_concept_revision_id,
            item.to_concept_revision_id,
            relation_type,
        )
        if key in relation_keys:
            raise AppError(
                "知识网络包含重复关系",
                code="KNOWLEDGE_NETWORK_RELATION_DUPLICATE",
                status=500,
            )
        relation_keys.add(key)
    if source_release_ids:
        releases = db.scalars(
            select(KnowledgeGraphRelease).where(
                KnowledgeGraphRelease.id.in_(source_release_ids)
            )
        ).all()
        if len(releases) != len(source_release_ids) or any(
            item.status != "published" for item in releases
        ):
            raise AppError(
                "知识网络只能引用已发布的知识图谱来源",
                code="KNOWLEDGE_NETWORK_SOURCE_NOT_PUBLISHED",
                status=500,
            )


def validate_knowledge_network(
    db: Session, *, knowledge_network_revision_id: str
) -> KnowledgeNetworkRevision:
    """Verify that exact network members still match the immutable content hash."""

    revision = db.get(KnowledgeNetworkRevision, knowledge_network_revision_id)
    network = (
        db.get(KnowledgeNetwork, revision.knowledge_network_id)
        if revision is not None
        else None
    )
    if revision is None or network is None or revision.status not in NETWORK_STATUSES:
        raise AppError(
            "知识网络版本不存在或状态无效",
            code="KNOWLEDGE_NETWORK_REVISION_INVALID",
            status=500,
        )
    concept_ids = db.scalars(
        select(KnowledgeNetworkConceptBinding.concept_revision_id)
        .where(
            KnowledgeNetworkConceptBinding.knowledge_network_revision_id
            == revision.id
        )
        .order_by(KnowledgeNetworkConceptBinding.position)
    ).all()
    relation_rows = db.scalars(
        select(KnowledgeRelationRevision)
        .join(
            KnowledgeNetworkRelationBinding,
            KnowledgeNetworkRelationBinding.knowledge_relation_revision_id
            == KnowledgeRelationRevision.id,
        )
        .where(
            KnowledgeNetworkRelationBinding.knowledge_network_revision_id
            == revision.id
        )
        .order_by(KnowledgeNetworkRelationBinding.position)
    ).all()
    if not concept_ids:
        raise AppError(
            "知识网络缺少知识节点",
            code="KNOWLEDGE_NETWORK_EMPTY",
            status=500,
        )
    relation_payloads: list[dict] = []
    for relation in relation_rows:
        relation_identity = db.get(
            KnowledgeRelation, relation.knowledge_relation_id
        )
        if relation_identity is None:
            raise AppError(
                "知识关系身份不存在",
                code="KNOWLEDGE_RELATION_IDENTITY_MISSING",
                status=500,
            )
        payload = {
            "fromConceptRevisionId": relation.from_concept_revision_id,
            "toConceptRevisionId": relation.to_concept_revision_id,
            "relationType": relation.relation_type,
            "statement": relation.statement,
            "scope": json.loads(relation.scope_json or "{}"),
            "provenance": json.loads(relation.provenance_json or "{}"),
        }
        expected_relation_hash = _hash(
            {
                "ruleVersion": KNOWLEDGE_NETWORK_RULE_VERSION,
                "namespace": relation_identity.namespace,
                **payload,
            }
        )
        if relation.content_hash != expected_relation_hash:
            raise AppError(
                "知识关系内容与冻结版本不一致",
                code="KNOWLEDGE_RELATION_HASH_MISMATCH",
                status=500,
            )
        relation_payloads.append(payload)
    expected_network_hash = _hash(
        {
            "ruleVersion": KNOWLEDGE_NETWORK_RULE_VERSION,
            "namespace": network.namespace,
            "label": network.label,
            "conceptRevisionIds": list(concept_ids),
            "relations": relation_payloads,
            "boundary": json.loads(revision.boundary_json or "{}"),
            "sourceReleaseIds": json.loads(
                revision.source_release_ids_json or "[]"
            ),
            "status": revision.status,
            "provenanceMode": revision.provenance_mode,
        }
    )
    if revision.content_hash != expected_network_hash:
        raise AppError(
            "知识网络内容与冻结版本不一致",
            code="KNOWLEDGE_NETWORK_HASH_MISMATCH",
            status=500,
        )
    return revision


def freeze_knowledge_network(
    db: Session,
    *,
    namespace: str,
    label: str,
    concept_revision_ids: list[str] | tuple[str, ...],
    relations: list[KnowledgeRelationSpec] | tuple[KnowledgeRelationSpec, ...] = (),
    boundary: dict | None = None,
    source_release_ids: list[str] | tuple[str, ...] = (),
    status: str = "route_scoped",
    provenance_mode: str = "route_scoped",
) -> FrozenKnowledgeNetwork:
    """Freeze exact nodes and exact relation meanings as one network revision."""

    concept_ids = tuple(concept_revision_ids)
    relation_specs = tuple(relations)
    release_ids = tuple(source_release_ids)
    _validate_inputs(
        db,
        concept_revision_ids=concept_ids,
        relations=relation_specs,
        status=status,
        source_release_ids=release_ids,
    )
    relation_payloads: list[dict] = []
    reused_relation_revisions: list[KnowledgeRelationRevision | None] = []
    for spec in relation_specs:
        if spec.reuse_relation_revision_id:
            reused = db.get(
                KnowledgeRelationRevision, spec.reuse_relation_revision_id
            )
            if reused is None or reused.verification_status not in {
                "published",
                "reviewed",
            }:
                raise AppError(
                    "知识网络引用了不可复用的关系版本",
                    code="KNOWLEDGE_RELATION_REUSE_NOT_PUBLISHED",
                    status=409,
                )
            expected = _normalized_relation_payload(spec)
            actual = {
                "fromConceptRevisionId": reused.from_concept_revision_id,
                "toConceptRevisionId": reused.to_concept_revision_id,
                "relationType": reused.relation_type,
                "statement": reused.statement,
                "scope": json.loads(reused.scope_json or "{}"),
                "provenance": json.loads(reused.provenance_json or "{}"),
            }
            if {
                key: actual[key]
                for key in (
                    "fromConceptRevisionId",
                    "toConceptRevisionId",
                    "relationType",
                    "statement",
                    "scope",
                )
            } != {
                key: expected[key]
                for key in (
                    "fromConceptRevisionId",
                    "toConceptRevisionId",
                    "relationType",
                    "statement",
                    "scope",
                )
            }:
                raise AppError(
                    "复用的知识关系与候选语义不一致",
                    code="KNOWLEDGE_RELATION_REUSE_SEMANTIC_MISMATCH",
                    status=409,
                )
            relation_payloads.append(actual)
            reused_relation_revisions.append(reused)
        else:
            relation_payloads.append(_normalized_relation_payload(spec))
            reused_relation_revisions.append(None)
    relation_payloads = tuple(relation_payloads)
    payload = {
        "ruleVersion": KNOWLEDGE_NETWORK_RULE_VERSION,
        "namespace": namespace,
        "label": " ".join(label.split()),
        "conceptRevisionIds": list(concept_ids),
        "relations": list(relation_payloads),
        "boundary": boundary or {},
        "sourceReleaseIds": list(release_ids),
        "status": status,
        "provenanceMode": provenance_mode,
    }
    content_hash = _hash(payload)
    existing_revision = db.scalar(
        select(KnowledgeNetworkRevision).where(
            KnowledgeNetworkRevision.content_hash == content_hash
        )
    )
    if existing_revision is not None:
        relation_rows = db.scalars(
            select(KnowledgeRelationRevision)
            .join(
                KnowledgeNetworkRelationBinding,
                KnowledgeNetworkRelationBinding.knowledge_relation_revision_id
                == KnowledgeRelationRevision.id,
            )
            .where(
                KnowledgeNetworkRelationBinding.knowledge_network_revision_id
                == existing_revision.id
            )
            .order_by(KnowledgeNetworkRelationBinding.position)
        ).all()
        return FrozenKnowledgeNetwork(existing_revision, tuple(relation_rows))

    network_key = content_hash[:40]
    network_id = _stable_id("knowledge_network", namespace, network_key)
    network = db.get(KnowledgeNetwork, network_id)
    if network is None:
        network = KnowledgeNetwork(
            id=network_id,
            namespace=namespace,
            network_key=network_key,
            label=" ".join(label.split()),
            status="active",
            origin=provenance_mode,
        )
        db.add(network)
        db.flush()
    network_revision_id = _stable_id("knowledge_network_revision", network_id, 1)
    network_revision = KnowledgeNetworkRevision(
        id=network_revision_id,
        knowledge_network_id=network_id,
        revision=1,
        status=status,
        provenance_mode=provenance_mode,
        source_release_ids_json=_dump(list(release_ids)),
        boundary_json=_dump(boundary or {}),
        content_hash=content_hash,
    )
    db.add(network_revision)
    db.flush()
    for position, concept_revision_id in enumerate(concept_ids, start=1):
        db.add(
            KnowledgeNetworkConceptBinding(
                id=_stable_id(
                    "knowledge_network_concept",
                    network_revision_id,
                    concept_revision_id,
                ),
                knowledge_network_revision_id=network_revision_id,
                concept_revision_id=concept_revision_id,
                position=position,
            )
        )

    relation_revisions: list[KnowledgeRelationRevision] = []
    for position, (spec, relation_payload, reused_relation) in enumerate(
        zip(
            relation_specs,
            relation_payloads,
            reused_relation_revisions,
            strict=True,
        ),
        start=1,
    ):
        if reused_relation is not None:
            relation_revision = reused_relation
            relation_revision_id = reused_relation.id
            db.add(
                KnowledgeNetworkRelationBinding(
                    id=_stable_id(
                        "knowledge_network_relation",
                        network_revision_id,
                        relation_revision_id,
                    ),
                    knowledge_network_revision_id=network_revision_id,
                    knowledge_relation_revision_id=relation_revision_id,
                    position=position,
                )
            )
            relation_revisions.append(relation_revision)
            continue
        relation_hash = _hash(
            {
                "ruleVersion": KNOWLEDGE_NETWORK_RULE_VERSION,
                "namespace": namespace,
                **relation_payload,
            }
        )
        relation_key = relation_hash[:40]
        relation_id = _stable_id("knowledge_relation", namespace, relation_key)
        relation = db.get(KnowledgeRelation, relation_id)
        if relation is None:
            relation = KnowledgeRelation(
                id=relation_id,
                namespace=namespace,
                relation_key=relation_key,
                status="active",
                origin=provenance_mode,
            )
            db.add(relation)
            db.flush()
        relation_revision_id = _stable_id(
            "knowledge_relation_revision", relation_id, 1
        )
        relation_revision = db.get(
            KnowledgeRelationRevision, relation_revision_id
        )
        if relation_revision is None:
            relation_revision = KnowledgeRelationRevision(
                id=relation_revision_id,
                knowledge_relation_id=relation_id,
                revision=1,
                from_concept_revision_id=spec.from_concept_revision_id,
                to_concept_revision_id=spec.to_concept_revision_id,
                relation_type=spec.relation_type.strip(),
                statement=" ".join(spec.statement.split()),
                scope_json=_dump(spec.scope or {}),
                provenance_json=_dump(spec.provenance or {}),
                verification_status=status,
                content_hash=relation_hash,
            )
            db.add(relation_revision)
            db.flush()
        db.add(
            KnowledgeNetworkRelationBinding(
                id=_stable_id(
                    "knowledge_network_relation",
                    network_revision_id,
                    relation_revision_id,
                ),
                knowledge_network_revision_id=network_revision_id,
                knowledge_relation_revision_id=relation_revision_id,
                position=position,
            )
        )
        relation_revisions.append(relation_revision)
    db.flush()
    return FrozenKnowledgeNetwork(network_revision, tuple(relation_revisions))
