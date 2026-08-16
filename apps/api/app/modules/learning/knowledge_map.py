"""User-owned capability map projected from stable knowledge subnets."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    Book,
    Capability,
    CapabilityConceptBinding,
    CapabilityRelationRequirement,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    Chapter,
    ConceptRevision,
    KnowledgeRelationRevision,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    Section,
    Series,
    Shelf,
)
from .capability_profiles import (
    CAPABILITY_PROJECTION_RULE_VERSION,
    STAGE_LABELS,
    STAGE_ORDER,
)
from .knowledge_profile import learner_knowledge_profile_view


KNOWLEDGE_MAP_RULE_VERSION = "personal_capability_map_v2"
RELATION_LABELS = {
    "prerequisite_for": "是前置",
    "applies_to": "可用于",
    "contrasts_with": "可对照",
    "refines": "进一步细化",
    "part_of": "组成",
}


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class KnowledgeMapService:
    def __init__(self, db: Session, *, user_id: str):
        self.db = db
        self.user_id = user_id

    def _owned_series(self, series_id: str | None) -> list[tuple[Series, Shelf]]:
        query = (
            select(Series, Shelf)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Shelf.user_id == self.user_id,
                Shelf.deleted_at.is_(None),
                Series.deleted_at.is_(None),
            )
            .order_by(Shelf.name, Series.title, Series.id)
        )
        if series_id:
            query = query.where(Series.id == series_id)
        rows = self.db.execute(query).all()
        if series_id and not rows:
            raise AppError("系列不存在", code="SERIES_NOT_FOUND", status=404)
        return rows

    def view(self, *, series_id: str | None = None) -> dict:
        owned_series = self._owned_series(series_id)
        series_ids = {series.id for series, _shelf in owned_series}
        series_meta = {
            series.id: {
                "id": series.id,
                "title": series.title,
                "shelfId": shelf.id,
                "shelfName": shelf.name,
            }
            for series, shelf in owned_series
        }
        if not series_ids:
            return self._empty(series_meta, series_id=series_id)

        route_rows = self.db.execute(
            select(CapabilityRouteBinding, CapabilityRevision, Capability)
            .join(
                CapabilityRevision,
                CapabilityRevision.id == CapabilityRouteBinding.capability_revision_id,
            )
            .join(Capability, Capability.id == CapabilityRevision.capability_id)
            .where(
                CapabilityRouteBinding.series_id.in_(series_ids),
                CapabilityRouteBinding.status == "active",
            )
            .order_by(
                CapabilityRouteBinding.series_id,
                CapabilityRevision.label,
                CapabilityRevision.id,
            )
        ).all()
        if not route_rows:
            return self._empty(series_meta, series_id=series_id)

        capability_ids = {revision.id for _route, revision, _capability in route_rows}
        states = {
            item.capability_revision_id: item
            for item in self.db.scalars(
                select(CapabilityStateProjection).where(
                    CapabilityStateProjection.user_id == self.user_id,
                    CapabilityStateProjection.capability_revision_id.in_(capability_ids),
                )
            ).all()
        }
        criteria_by_capability: dict[str, list[CapabilityStageCriterion]] = defaultdict(list)
        for item in self.db.scalars(
            select(CapabilityStageCriterion)
            .where(CapabilityStageCriterion.capability_revision_id.in_(capability_ids))
            .order_by(
                CapabilityStageCriterion.capability_revision_id,
                CapabilityStageCriterion.position,
                CapabilityStageCriterion.id,
            )
        ).all():
            criteria_by_capability[item.capability_revision_id].append(item)
        for criteria in criteria_by_capability.values():
            criteria.sort(key=lambda item: (
                STAGE_ORDER.get(item.stage, 99), item.position, item.id
            ))

        concept_rows = self.db.execute(
            select(CapabilityConceptBinding, ConceptRevision)
            .join(
                ConceptRevision,
                ConceptRevision.id == CapabilityConceptBinding.concept_revision_id,
            )
            .where(CapabilityConceptBinding.capability_revision_id.in_(capability_ids))
            .order_by(
                CapabilityConceptBinding.capability_revision_id,
                CapabilityConceptBinding.position,
                CapabilityConceptBinding.id,
            )
        ).all()
        knowledge_by_capability: dict[str, list[dict]] = defaultdict(list)
        capabilities_by_concept: dict[str, set[str]] = defaultdict(set)
        concept_labels: dict[str, str] = {}
        for binding, concept in concept_rows:
            knowledge_by_capability[binding.capability_revision_id].append({
                "conceptRevisionId": concept.id,
                "label": concept.label,
                "role": binding.role,
                "required": binding.required,
            })
            capabilities_by_concept[concept.id].add(binding.capability_revision_id)
            concept_labels[concept.id] = concept.label

        relation_rows = self.db.execute(
            select(CapabilityRelationRequirement, KnowledgeRelationRevision)
            .join(
                KnowledgeRelationRevision,
                KnowledgeRelationRevision.id
                == CapabilityRelationRequirement.knowledge_relation_revision_id,
            )
            .where(
                CapabilityRelationRequirement.capability_revision_id.in_(capability_ids)
            )
            .order_by(
                CapabilityRelationRequirement.capability_revision_id,
                CapabilityRelationRequirement.position,
                CapabilityRelationRequirement.id,
            )
        ).all()
        relations_by_capability: dict[str, list[dict]] = defaultdict(list)
        for requirement, relation in relation_rows:
            relations_by_capability[requirement.capability_revision_id].append({
                "knowledgeRelationRevisionId": relation.id,
                "fromConceptRevisionId": relation.from_concept_revision_id,
                "toConceptRevisionId": relation.to_concept_revision_id,
                "type": relation.relation_type,
                "label": RELATION_LABELS.get(relation.relation_type, "相关"),
                "statement": relation.statement,
                "required": requirement.required,
                "minimumStage": requirement.minimum_stage,
            })

        target_rows = self.db.execute(
            select(
                AssessmentTarget,
                LearningContractAssessmentTarget,
                LearningContractVersion,
                Section,
                Chapter,
                Book,
                Series,
            )
            .join(
                LearningContractAssessmentTarget,
                LearningContractAssessmentTarget.assessment_target_id == AssessmentTarget.id,
            )
            .join(
                LearningContractVersion,
                LearningContractVersion.id
                == LearningContractAssessmentTarget.contract_version_id,
            )
            .join(Section, Section.id == LearningContractVersion.section_id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(Series, Series.id == Book.series_id)
            .where(
                Series.id.in_(series_ids),
                AssessmentTarget.status == "active",
                AssessmentTarget.capability_revision_id.in_(capability_ids),
            )
            .order_by(
                Series.id,
                Book.position,
                Chapter.position,
                Section.position,
                LearningContractVersion.version.desc(),
            )
        ).all()
        contexts_by_capability: dict[str, list[dict]] = defaultdict(list)
        target_ids_by_capability: dict[str, set[str]] = defaultdict(set)
        seen_contexts: set[tuple[str, str, str]] = set()
        for target, binding, contract, section, chapter, book, series in target_rows:
            capability_id = target.capability_revision_id
            if not capability_id:
                continue
            target_ids_by_capability[capability_id].add(target.id)
            key = (capability_id, series.id, section.id)
            if key in seen_contexts:
                continue
            seen_contexts.add(key)
            contexts_by_capability[capability_id].append({
                "seriesId": series.id,
                "seriesTitle": series.title,
                "bookId": book.id,
                "bookTitle": book.title,
                "chapterId": chapter.id,
                "chapterTitle": chapter.title,
                "sectionId": section.id,
                "sectionTitle": section.title,
                "required": binding.required,
                "contractVersionId": contract.id,
            })

        nodes_by_id: dict[str, dict] = {}
        route_stage_by_capability = {
            capability_id: max(
                (
                    candidate.target_stage
                    for candidate, candidate_revision, _candidate_capability in route_rows
                    if candidate_revision.id == capability_id
                ),
                key=lambda stage: STAGE_ORDER.get(stage, 0),
            )
            for capability_id in capability_ids
        }
        for route, revision, capability in route_rows:
            route_stage = route_stage_by_capability[revision.id]
            state = states.get(revision.id)
            current_stage = state.current_stage if state else "unranked"
            satisfied = set(_load(state.satisfied_criterion_ids_json, [])) if state else set()
            missing = [
                item for item in criteria_by_capability[revision.id]
                if item.required
                and item.id not in satisfied
                and STAGE_ORDER.get(item.stage, 99) <= STAGE_ORDER.get(route_stage, 0)
            ]
            next_criterion = missing[0] if missing else None
            activation = state.activation_state if state else "learning"
            if activation == "due_for_reactivation":
                next_action = {"kind": "wake", "label": "用一次短复习唤醒"}
            elif current_stage == "unranked":
                next_action = {"kind": "learn", "label": "完成首次正式验证"}
            elif STAGE_ORDER.get(current_stage, 0) >= STAGE_ORDER.get(route_stage, 0):
                next_action = {"kind": "maintain", "label": "本路线阶段已达成"}
            else:
                label = STAGE_LABELS.get(next_criterion.stage, "下一阶") if next_criterion else "下一阶"
                next_action = {"kind": "advance", "label": f"进入{label}任务"}
            existing = nodes_by_id.get(revision.id)
            route_contexts = contexts_by_capability[revision.id]
            if existing:
                existing["routeContexts"] = [
                    *existing["routeContexts"],
                    *[
                        item for item in route_contexts
                        if (item["seriesId"], item["sectionId"])
                        not in {
                            (seen["seriesId"], seen["sectionId"])
                            for seen in existing["routeContexts"]
                        }
                    ],
                ]
                continue
            nodes_by_id[revision.id] = {
                "capabilityRevisionId": revision.id,
                "capabilityId": capability.id,
                "label": revision.label or capability.canonical_name,
                "stage": current_stage,
                "stageOrder": STAGE_ORDER.get(current_stage, 0),
                "stageLabel": STAGE_LABELS.get(current_stage, STAGE_LABELS["unranked"]),
                "naturalStageCeiling": revision.natural_stage_ceiling,
                "naturalStageCeilingLabel": STAGE_LABELS.get(
                    revision.natural_stage_ceiling, revision.natural_stage_ceiling
                ),
                "routeStageCeiling": route_stage,
                "routeStageCeilingLabel": STAGE_LABELS.get(route_stage, route_stage),
                "activationState": activation,
                "stabilityDays": state.stability_days if state else 0,
                "nextDueAt": state.next_due_at.isoformat() if state and state.next_due_at else None,
                "evidenceCount": state.evidence_count if state else 0,
                "independentEvidenceCount": state.independent_evidence_count if state else 0,
                "targetCount": len(target_ids_by_capability[revision.id]),
                "knowledge": knowledge_by_capability[revision.id],
                "relations": relations_by_capability[revision.id],
                "routeContexts": route_contexts,
                "nextStage": next_criterion.stage if next_criterion else None,
                "nextCriterion": next_criterion.statement if next_criterion else None,
                "nextAction": next_action,
            }

        edges = []
        seen_edges: set[tuple[str, str, str]] = set()
        for concept_id, related_capabilities in sorted(capabilities_by_concept.items()):
            ordered = sorted(related_capabilities & set(nodes_by_id))
            for index, left in enumerate(ordered):
                for right in ordered[index + 1:]:
                    key = (left, right, concept_id)
                    if key in seen_edges:
                        continue
                    seen_edges.add(key)
                    edges.append({
                        "id": f"shared:{concept_id}:{left}:{right}",
                        "from": left,
                        "to": right,
                        "type": "shared_knowledge",
                        "label": f"共享 · {concept_labels[concept_id]}",
                    })

        nodes = sorted(
            nodes_by_id.values(),
            key=lambda item: (item["stageOrder"], item["label"], item["capabilityRevisionId"]),
        )
        stage_counts = Counter(item["stage"] for item in nodes)
        active_count = sum(item["activationState"] == "available" for item in nodes)
        due_count = sum(item["activationState"] == "due_for_reactivation" for item in nodes)
        staged_count = sum(item["stageOrder"] > 0 for item in nodes)
        profile = learner_knowledge_profile_view(self.db, user_id=self.user_id)
        return {
            "schemaVersion": "personal_capability_map_v2",
            "ruleVersion": KNOWLEDGE_MAP_RULE_VERSION,
            "projectionRuleVersion": CAPABILITY_PROJECTION_RULE_VERSION,
            "availability": "ready" if nodes else "not_ready",
            "scope": {
                "seriesId": series_id,
                "series": list(series_meta.values()),
                "definition": "当前路线中的稳定能力及其正式知识子网",
            },
            "progress": {
                "stagedCapabilities": staged_count,
                "requiredCapabilities": len(nodes),
                "coveragePpm": round(staged_count / len(nodes) * 1_000_000) if nodes else 0,
                "activeCapabilities": active_count,
                "needsWakeCapabilities": due_count,
                "learningCapabilities": len(nodes) - active_count - due_count,
                "stageCounts": dict(sorted(stage_counts.items())),
                "basis": "capability_routes_and_qualified_evidence",
            },
            "learnerProfile": profile.get("capabilityProfile", {}),
            "nodes": nodes,
            "edges": edges,
            "excluded": {
                "targetWithoutCapabilityCount": int(self.db.scalar(
                    select(func.count(func.distinct(AssessmentTarget.id)))
                    .join(
                        LearningContractAssessmentTarget,
                        LearningContractAssessmentTarget.assessment_target_id
                        == AssessmentTarget.id,
                    )
                    .join(
                        LearningContractVersion,
                        LearningContractVersion.id
                        == LearningContractAssessmentTarget.contract_version_id,
                    )
                    .join(Section, Section.id == LearningContractVersion.section_id)
                    .join(Chapter, Chapter.id == Section.chapter_id)
                    .join(Book, Book.id == Chapter.book_id)
                    .join(Series, Series.id == Book.series_id)
                    .where(
                        Series.id.in_(series_ids),
                        AssessmentTarget.capability_revision_id.is_(None),
                        AssessmentTarget.status == "active",
                    )
                ) or 0),
            },
            "message": "这里展示稳定能力，而不是把每个知识点各自做成一枚段位。展开能力即可看到它依赖的知识节点与关系。",
        }

    @staticmethod
    def _empty(series_meta: dict[str, dict], *, series_id: str | None) -> dict:
        return {
            "schemaVersion": "personal_capability_map_v2",
            "ruleVersion": KNOWLEDGE_MAP_RULE_VERSION,
            "projectionRuleVersion": CAPABILITY_PROJECTION_RULE_VERSION,
            "availability": "not_ready",
            "scope": {
                "seriesId": series_id,
                "series": list(series_meta.values()),
                "definition": "当前路线中的稳定能力及其正式知识子网",
            },
            "progress": {
                "stagedCapabilities": 0,
                "requiredCapabilities": 0,
                "coveragePpm": 0,
                "activeCapabilities": 0,
                "needsWakeCapabilities": 0,
                "learningCapabilities": 0,
                "stageCounts": {},
                "basis": "capability_routes_and_qualified_evidence",
            },
            "learnerProfile": {},
            "nodes": [],
            "edges": [],
            "excluded": {"targetWithoutCapabilityCount": 0},
            "message": "完成带有稳定能力身份的小节后，能力版图会从这里生长。",
        }
