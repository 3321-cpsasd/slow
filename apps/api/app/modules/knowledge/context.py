from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...ai.context import KnowledgeContextBudget, KnowledgeContextPack
from ...core.errors import AppError
from ...infrastructure.tables import (
    Concept,
    ConceptRelationVersion,
    ConceptRevision,
    KnowledgeClaimBinding,
    KnowledgeGraphRelease,
    KnowledgeSourceVersion,
    LearningContractConcept,
    LearningContractVersion,
    Series,
    SeriesCurriculumBaselineBinding,
    SourceClaimVersion,
)


KNOWLEDGE_CONTEXT_SCHEMA_VERSION = "knowledge_context_pack_v1"
KNOWLEDGE_RETRIEVAL_RULE_VERSION = "published_bounded_bfs_v1"
DEFAULT_KNOWLEDGE_BUDGET = KnowledgeContextBudget(
    maxNodes=12,
    maxEdges=18,
    maxHops=2,
)
PRE_CONTRACT_OPERATIONS = frozenset({"plan", "book_replan", "chapter"})


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_ids(manifest: dict[str, Any], key: str) -> list[str]:
    value = manifest.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AppError(
            "正式知识图发布清单不完整，不能用于生成",
            code="KNOWLEDGE_GRAPH_MANIFEST_INVALID",
            status=500,
            details={"field": key},
        )
    return list(dict.fromkeys(value))


@dataclass(frozen=True)
class PublishedGraphSnapshot:
    release: KnowledgeGraphRelease
    manifest: dict[str, Any]
    concepts: dict[str, tuple[ConceptRevision, Concept]]
    relations: tuple[ConceptRelationVersion, ...]
    claims: tuple[SourceClaimVersion, ...]
    claim_bindings: tuple[KnowledgeClaimBinding, ...]
    sources: dict[str, KnowledgeSourceVersion]


class KnowledgeGraphRepository:
    """Small relational repository; graph traversal remains deterministic Python."""

    def __init__(self, db: Session):
        self.db = db

    def published_release(self, baseline_version_id: str) -> KnowledgeGraphRelease | None:
        return self.db.scalar(
            select(KnowledgeGraphRelease)
            .where(
                KnowledgeGraphRelease.baseline_version_id == baseline_version_id,
                KnowledgeGraphRelease.status == "published",
            )
            .order_by(KnowledgeGraphRelease.version.desc())
        )

    def load_published_snapshot(
        self,
        release: KnowledgeGraphRelease,
    ) -> PublishedGraphSnapshot:
        manifest = _load(release.manifest_json, None)
        if not isinstance(manifest, dict):
            raise AppError(
                "正式知识图发布清单无法解析，不能用于生成",
                code="KNOWLEDGE_GRAPH_MANIFEST_INVALID",
                status=500,
            )
        concept_ids = _manifest_ids(manifest, "conceptRevisionIds")
        relation_ids = _manifest_ids(manifest, "relationVersionIds")
        claim_ids = _manifest_ids(manifest, "claimVersionIds")
        source_ids = _manifest_ids(manifest, "sourceVersionIds")
        claim_binding_ids = _manifest_ids(manifest, "claimBindingIds")
        if not concept_ids:
            raise AppError(
                "正式知识图没有已审核概念，不能用于生成",
                code="KNOWLEDGE_GRAPH_MANIFEST_INVALID",
                status=500,
                details={"field": "conceptRevisionIds"},
            )

        concept_rows = self.db.execute(
            select(ConceptRevision, Concept)
            .join(Concept, Concept.id == ConceptRevision.concept_id)
            .where(ConceptRevision.id.in_(concept_ids))
        ).all()
        concepts = {revision.id: (revision, concept) for revision, concept in concept_rows}
        invalid_concepts = [
            item
            for item in concept_ids
            if item not in concepts
            or concepts[item][0].verification_status != "reviewed"
            or concepts[item][1].status != "active"
        ]
        if invalid_concepts:
            raise AppError(
                "正式知识图引用了未审核或不可用的概念版本",
                code="KNOWLEDGE_GRAPH_CONCEPT_UNPUBLISHED",
                status=500,
                details={"conceptRevisionIds": invalid_concepts},
            )

        relations = tuple(
            self.db.scalars(
                select(ConceptRelationVersion)
                .where(ConceptRelationVersion.id.in_(relation_ids))
                .order_by(ConceptRelationVersion.id)
            ).all()
        )
        invalid_relation_ids = set(relation_ids) - {item.id for item in relations}
        invalid_relation_ids.update(
            item.id
            for item in relations
            if item.release_id != release.id
            or item.status != "published"
            or item.from_concept_revision_id not in concepts
            or item.to_concept_revision_id not in concepts
        )
        if invalid_relation_ids:
            raise AppError(
                "正式知识图引用了未发布或越界的关系版本",
                code="KNOWLEDGE_GRAPH_RELATION_UNPUBLISHED",
                status=500,
                details={"relationVersionIds": sorted(invalid_relation_ids)},
            )

        claims = tuple(
            self.db.scalars(
                select(SourceClaimVersion)
                .where(SourceClaimVersion.id.in_(claim_ids))
                .order_by(SourceClaimVersion.id)
            ).all()
        )
        invalid_claim_ids = set(claim_ids) - {item.id for item in claims}
        invalid_claim_ids.update(
            item.id
            for item in claims
            if item.status != "published"
            or item.trust_state != "verified"
            or not isinstance(_load(item.scope_json, None), dict)
            or not isinstance(
                _load(item.scope_json, {}).get("conceptRevisionIds"), list
            )
            or not _load(item.scope_json, {}).get("conceptRevisionIds")
            or not set(
                _load(item.scope_json, {}).get("conceptRevisionIds", [])
            ).issubset(concepts)
        )
        if invalid_claim_ids:
            raise AppError(
                "正式知识图引用了未核验的知识主张",
                code="KNOWLEDGE_GRAPH_CLAIM_UNPUBLISHED",
                status=500,
                details={"claimVersionIds": sorted(invalid_claim_ids)},
            )

        sources = {
            item.id: item
            for item in self.db.scalars(
                select(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.id.in_(source_ids)
                )
            ).all()
        }
        if set(source_ids) != set(sources):
            raise AppError(
                "正式知识图引用了不存在的来源版本",
                code="KNOWLEDGE_GRAPH_SOURCE_INVALID",
                status=500,
                details={"sourceVersionIds": sorted(set(source_ids) - set(sources))},
            )
        invalid_sources = [
            item.id
            for item in sources.values()
            if item.verification_status != "verified"
        ]
        if invalid_sources:
            raise AppError(
                "正式知识图引用了未核验的来源版本",
                code="KNOWLEDGE_GRAPH_SOURCE_INVALID",
                status=500,
                details={"sourceVersionIds": sorted(invalid_sources)},
            )

        claim_bindings = tuple(
            self.db.scalars(
                select(KnowledgeClaimBinding)
                .where(KnowledgeClaimBinding.id.in_(claim_binding_ids))
                .order_by(KnowledgeClaimBinding.id)
            ).all()
        )
        invalid_binding_ids = set(claim_binding_ids) - {
            item.id for item in claim_bindings
        }
        invalid_binding_ids.update(
            item.id
            for item in claim_bindings
            if item.release_id != release.id
            or item.source_claim_version_id not in {claim.id for claim in claims}
            or item.knowledge_source_version_id not in sources
            or item.verification_status != "verified"
        )
        bound_claim_ids = {
            item.source_claim_version_id for item in claim_bindings
        }
        invalid_binding_ids.update(
            f"missing_for_claim:{claim.id}"
            for claim in claims
            if claim.id not in bound_claim_ids
        )
        if invalid_binding_ids:
            raise AppError(
                "正式知识图引用了未核验或越界的主张绑定",
                code="KNOWLEDGE_GRAPH_CLAIM_BINDING_INVALID",
                status=500,
                details={"claimBindingIds": sorted(invalid_binding_ids)},
            )

        return PublishedGraphSnapshot(
            release=release,
            manifest=manifest,
            concepts=concepts,
            relations=relations,
            claims=claims,
            claim_bindings=claim_bindings,
            sources=sources,
        )


class KnowledgeContextBuilder:
    def __init__(self, db: Session):
        self.db = db
        self.repository = KnowledgeGraphRepository(db)

    def build(
        self,
        *,
        operation: str,
        series: Series | None,
        contract: LearningContractVersion | None,
        budget: KnowledgeContextBudget = DEFAULT_KNOWLEDGE_BUDGET,
    ) -> KnowledgeContextPack:
        binding = (
            self.db.scalar(
                select(SeriesCurriculumBaselineBinding).where(
                    SeriesCurriculumBaselineBinding.series_id == series.id
                )
            )
            if series
            else None
        )
        if not binding:
            return self._pack(
                status="not_applicable",
                reason="series_has_no_published_curriculum_baseline_binding",
                budget=budget,
            )
        if contract is None:
            if operation in PRE_CONTRACT_OPERATIONS:
                return self._pack(
                    status="not_applicable",
                    reason="pre_contract_operation_uses_curriculum_authority_only",
                    budget=budget,
                    baseline_version_id=binding.baseline_version_id,
                )
            raise AppError(
                "正式课程生成缺少 Learning Contract，不能检索知识上下文",
                code="KNOWLEDGE_CONTEXT_CONTRACT_MISSING",
                status=409,
            )
        release = self.repository.published_release(binding.baseline_version_id)
        if release is None:
            raise AppError(
                "当前课程基准没有已发布知识图，正文生成已停止",
                code="KNOWLEDGE_GRAPH_RELEASE_MISSING",
                status=409,
                details={"baselineVersionId": binding.baseline_version_id},
            )
        snapshot = self.repository.load_published_snapshot(release)
        seed_rows = self.db.execute(
            select(LearningContractConcept, ConceptRevision)
            .join(
                ConceptRevision,
                ConceptRevision.id == LearningContractConcept.concept_revision_id,
            )
            .where(LearningContractConcept.contract_version_id == contract.id)
            .order_by(LearningContractConcept.position)
        ).all()
        if not seed_rows:
            raise AppError(
                "Learning Contract 没有显式概念版本，不能构建知识上下文",
                code="KNOWLEDGE_CONTEXT_SEEDS_MISSING",
                status=409,
            )
        seed_ids = list(
            dict.fromkeys(binding.concept_revision_id for binding, _ in seed_rows)
        )
        invalid_seeds = [
            revision.id
            for _, revision in seed_rows
            if revision.verification_status != "reviewed"
            or revision.id not in snapshot.concepts
        ]
        if invalid_seeds:
            raise AppError(
                "Learning Contract 引用了未审核或不属于当前知识图的概念版本",
                code="KNOWLEDGE_CONTEXT_SEED_UNPUBLISHED",
                status=409,
                details={"conceptRevisionIds": invalid_seeds},
            )
        if len(seed_ids) > budget.max_nodes:
            raise AppError(
                "知识上下文节点预算小于 Learning Contract 的必需概念数",
                code="KNOWLEDGE_CONTEXT_NODE_BUDGET_TOO_SMALL",
                status=409,
                details={
                    "requiredSeedCount": len(seed_ids),
                    "maxNodes": budget.max_nodes,
                },
            )
        return self._bounded_pack(snapshot, seed_ids=seed_ids, budget=budget)

    def _bounded_pack(
        self,
        snapshot: PublishedGraphSnapshot,
        *,
        seed_ids: list[str],
        budget: KnowledgeContextBudget,
    ) -> KnowledgeContextPack:
        adjacency: dict[str, list[tuple[ConceptRelationVersion, str]]] = defaultdict(list)
        for relation in snapshot.relations:
            adjacency[relation.from_concept_revision_id].append(
                (relation, relation.to_concept_revision_id)
            )
            adjacency[relation.to_concept_revision_id].append(
                (relation, relation.from_concept_revision_id)
            )
        for values in adjacency.values():
            values.sort(key=lambda item: (item[0].id, item[1]))

        distances = {item: 0 for item in seed_ids}
        node_ids = list(seed_ids)
        edge_ids: list[str] = []
        edge_by_id: dict[str, ConceptRelationVersion] = {}
        queue = deque(seed_ids)
        examined_edges: set[str] = set()
        omitted_nodes: set[str] = set()
        node_limited_nodes: set[str] = set()
        omitted_edges: set[str] = set()
        edge_limited_edges: set[str] = set()
        hop_limited_nodes: set[str] = set()
        while queue:
            current = queue.popleft()
            distance = distances[current]
            for relation, neighbor in adjacency.get(current, []):
                if relation.id in examined_edges:
                    continue
                examined_edges.add(relation.id)
                if neighbor not in distances and distance >= budget.max_hops:
                    hop_limited_nodes.add(neighbor)
                    omitted_edges.add(relation.id)
                    continue
                if neighbor not in distances and len(node_ids) >= budget.max_nodes:
                    omitted_nodes.add(neighbor)
                    node_limited_nodes.add(neighbor)
                    omitted_edges.add(relation.id)
                    continue
                if len(edge_ids) >= budget.max_edges:
                    omitted_edges.add(relation.id)
                    edge_limited_edges.add(relation.id)
                    if neighbor not in distances:
                        omitted_nodes.add(neighbor)
                    continue
                if neighbor not in distances:
                    distances[neighbor] = distance + 1
                    node_ids.append(neighbor)
                    queue.append(neighbor)
                edge_ids.append(relation.id)
                edge_by_id[relation.id] = relation

        nodes = []
        for concept_revision_id in node_ids:
            revision, concept = snapshot.concepts[concept_revision_id]
            nodes.append(
                {
                    "conceptRevisionId": revision.id,
                    "conceptId": concept.id,
                    "conceptKey": concept.concept_key,
                    "label": revision.label,
                    "definition": revision.definition,
                    "scope": _load(revision.scope_json, {}),
                    "boundaries": _load(revision.boundaries_json, []),
                    "verificationStatus": revision.verification_status,
                    "distance": distances[revision.id],
                    "role": "seed" if revision.id in seed_ids else "related",
                }
            )
        edges = [
            {
                "relationVersionId": relation.id,
                "fromConceptRevisionId": relation.from_concept_revision_id,
                "toConceptRevisionId": relation.to_concept_revision_id,
                "relationType": relation.relation_type,
                "relationRevision": relation.relation_revision,
                "status": relation.status,
            }
            for relation in (edge_by_id[item] for item in edge_ids)
        ]
        bindings_by_claim: dict[str, list[KnowledgeClaimBinding]] = defaultdict(list)
        for binding in snapshot.claim_bindings:
            bindings_by_claim[binding.source_claim_version_id].append(binding)
        claims = []
        for claim in snapshot.claims:
            scope = _load(claim.scope_json, {})
            scoped_concepts = set(scope.get("conceptRevisionIds", []))
            if scoped_concepts and not scoped_concepts.intersection(node_ids):
                continue
            claims.append(
                {
                    "claimVersionId": claim.id,
                    "statement": claim.statement,
                    "claimKind": claim.claim_kind,
                    "scope": scope,
                    "trustState": claim.trust_state,
                    "sources": [
                        {
                            "claimBindingId": binding.id,
                            "sourceVersionId": binding.knowledge_source_version_id,
                            "sourceTitle": snapshot.sources[
                                binding.knowledge_source_version_id
                            ].title,
                            "sourceUrl": snapshot.sources[
                                binding.knowledge_source_version_id
                            ].url,
                            "versionLabel": snapshot.sources[
                                binding.knowledge_source_version_id
                            ].version_label,
                            "locatorType": binding.locator_type,
                            "locator": _load(binding.locator_json, {}),
                            "supportType": binding.support_type,
                            "verificationStatus": binding.verification_status,
                        }
                        for binding in bindings_by_claim[claim.id]
                    ],
                }
            )
        reasons = []
        for code, values in (
            ("maxNodes", node_limited_nodes),
            ("maxEdges", edge_limited_edges),
            ("maxHops", hop_limited_nodes),
        ):
            if values:
                reasons.append({"code": code, "omittedCount": len(values)})
        return self._pack(
            status="ready",
            reason="",
            budget=budget,
            release=snapshot.release,
            seed_ids=seed_ids,
            nodes=nodes,
            edges=edges,
            claims=claims,
            truncation={
                "truncated": bool(reasons),
                "reasons": reasons,
                "omittedNodeCount": len(omitted_nodes | hop_limited_nodes),
                "omittedEdgeCount": len(omitted_edges),
            },
        )

    @staticmethod
    def _pack(
        *,
        status: str,
        reason: str,
        budget: KnowledgeContextBudget,
        release: KnowledgeGraphRelease | None = None,
        baseline_version_id: str = "",
        seed_ids: list[str] | None = None,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        claims: list[dict[str, Any]] | None = None,
        truncation: dict[str, Any] | None = None,
    ) -> KnowledgeContextPack:
        payload = {
            "schemaVersion": KNOWLEDGE_CONTEXT_SCHEMA_VERSION,
            "status": status,
            "reason": reason,
            "releaseId": release.id if release else "",
            "releaseVersion": release.version if release else 0,
            "baselineVersionId": (
                release.baseline_version_id if release else baseline_version_id
            ),
            "retrievalRuleVersion": KNOWLEDGE_RETRIEVAL_RULE_VERSION,
            "budget": budget.model_dump(by_alias=True, mode="json"),
            "seedConceptRevisionIds": seed_ids or [],
            "nodes": nodes or [],
            "edges": edges or [],
            "claims": claims or [],
            "truncation": truncation
            or {
                "truncated": False,
                "reasons": [],
                "omittedNodeCount": 0,
                "omittedEdgeCount": 0,
            },
        }
        return KnowledgeContextPack.model_validate(
            {**payload, "contextHash": _canonical_hash(payload)}
        )
