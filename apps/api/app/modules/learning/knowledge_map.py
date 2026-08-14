"""User-owned, evidence-backed knowledge subgraph read model."""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    Book,
    Chapter,
    ConceptRelationVersion,
    KnowledgeGraphRelease,
    KnowledgeNodeStateProjection,
    KnowledgeStateProjection,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    QuizAttempt,
    ReviewAssignment,
    Section,
    Series,
    Shelf,
)
from .knowledge_profile import learner_knowledge_profile_view
from .knowledge_ranks import (
    KNOWLEDGE_RANK_RULE_VERSION,
    knowledge_node_views_for_concepts,
    resolve_effective_rank_target,
)


KNOWLEDGE_MAP_RULE_VERSION = "personal_knowledge_subgraph_v1"
VERIFIED_CLAIMS = frozenset({"verified_immediate", "verified_delayed", "retained"})
RELATION_LABELS = {
    "prerequisite_for": "是前置",
    "applies_to": "可用于",
    "contrasts_with": "可对照",
    "refines": "进一步细化",
    "part_of": "组成",
}


def _recommended_target_id(
    concept_target_ids: set[str],
    failed_review_target_ids: list[str],
) -> str:
    """Prefer the newest failed wake that can actually authorize reinforcement."""

    return next(
        (
            target_id
            for target_id in failed_review_target_ids
            if target_id in concept_target_ids
        ),
        sorted(concept_target_ids)[0] if concept_target_ids else "",
    )


def _next_action(node: dict) -> dict:
    activation = node["activation"]
    if activation == "reassessment":
        return {"kind": "reinforce", "label": "先补强这项能力"}
    if activation == "due":
        return {"kind": "wake", "label": "用一次短复习唤醒"}
    if node["rank"] == "unranked":
        return {"kind": "learn", "label": "完成首次正式验证"}
    if node["atCeiling"]:
        return {"kind": "maintain", "label": "已到本节点最高段位"}
    return {"kind": "advance", "label": "在更深情境中继续验证"}


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

        rows = self.db.execute(
            select(
                LearningContractVersion,
                LearningContractAssessmentTarget,
                AssessmentTarget,
                Section,
                Chapter,
                Book,
                Series,
            )
            .join(
                LearningContractAssessmentTarget,
                LearningContractAssessmentTarget.contract_version_id
                == LearningContractVersion.id,
            )
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == LearningContractAssessmentTarget.assessment_target_id,
            )
            .join(Section, Section.id == LearningContractVersion.section_id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(Series, Series.id == Book.series_id)
            .where(
                Series.id.in_(series_ids),
                Book.deleted_at.is_(None),
                AssessmentTarget.status == "active",
                LearningContractAssessmentTarget.diagnostic_only.is_(False),
            )
            .order_by(
                Series.id,
                Book.position,
                Chapter.position,
                Section.position,
                LearningContractVersion.version.desc(),
                LearningContractAssessmentTarget.position,
            )
        ).all()

        latest_contract_by_section: dict[str, tuple[int, str]] = {}
        for contract, _binding, _target, section, *_rest in rows:
            marker = latest_contract_by_section.get(section.id)
            if marker is None or contract.version > marker[0]:
                latest_contract_by_section[section.id] = (contract.version, contract.id)
        current_rows = [
            row
            for row in rows
            if latest_contract_by_section.get(row[3].id, (0, ""))[1] == row[0].id
        ]

        route_paths: dict[tuple[str, str], dict] = {}
        targets: dict[str, AssessmentTarget] = {}
        required_target_ids: set[str] = set()
        for contract, binding, target, section, chapter, book, series in current_rows:
            targets[target.id] = target
            if binding.required:
                required_target_ids.add(target.id)
            path = {
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
            }
            route_paths[(contract.id, target.id)] = path
        if not required_target_ids:
            required_target_ids = set(targets)

        formal_target_ids: set[str] = set()
        targets_by_concept: dict[str, set[str]] = defaultdict(set)
        paths_by_concept: dict[str, list[dict]] = defaultdict(list)
        for contract, _binding, target, *_rest in current_rows:
            effective = resolve_effective_rank_target(
                self.db,
                source_target=target,
                learning_contract_version_id=contract.id,
            )
            if effective is None or not effective.concept_revision_id:
                continue
            concept_id = effective.concept_revision_id
            formal_target_ids.add(target.id)
            targets_by_concept[concept_id].add(target.id)
            paths_by_concept[concept_id].append(
                route_paths[(contract.id, target.id)]
            )
        concept_ids = set(targets_by_concept)
        # The all-goals view is the user's durable subgraph, not merely the
        # union of today's active routes. Keep previously evidenced published
        # nodes visible across books and shelves; a series-filtered view stays
        # bounded to that route.
        if series_id is None:
            concept_ids.update(
                self.db.scalars(
                    select(KnowledgeNodeStateProjection.concept_revision_id).where(
                        KnowledgeNodeStateProjection.user_id == self.user_id,
                        KnowledgeNodeStateProjection.evidence_count > 0,
                    )
                ).all()
            )
        node_views = knowledge_node_views_for_concepts(
            self.db,
            user_id=self.user_id,
            concept_revision_ids=concept_ids,
        )
        state_by_target = {
            item.assessment_target_id: item
            for item in self.db.scalars(
                select(KnowledgeStateProjection).where(
                    KnowledgeStateProjection.user_id == self.user_id,
                    KnowledgeStateProjection.assessment_target_id.in_(set(targets)),
                )
            ).all()
        } if targets else {}
        # A node can represent several assessment targets. Reinforcement is only
        # authorized by a submitted failed wake, so an arbitrary target id can
        # make an otherwise actionable node return 409. Keep the query ordered
        # newest-first and select the first failed target belonging to each node.
        failed_review_target_ids = list(self.db.scalars(
            select(ReviewAssignment.assessment_target_id)
            .join(
                QuizAttempt,
                QuizAttempt.id == ReviewAssignment.submitted_attempt_id,
            )
            .where(
                ReviewAssignment.user_id == self.user_id,
                ReviewAssignment.assessment_target_id.in_(formal_target_ids),
                ReviewAssignment.status == "submitted",
                QuizAttempt.passed.is_(False),
            )
            .order_by(
                ReviewAssignment.updated_at.desc(),
                ReviewAssignment.id.desc(),
            )
        )) if formal_target_ids else []

        nodes = []
        for concept_id, node in sorted(
            node_views.items(),
            key=lambda item: (item[1]["rankOrder"], item[1]["label"], item[0]),
        ):
            concept_target_ids = targets_by_concept[concept_id]
            verified_ids = {
                target_id
                for target_id in concept_target_ids
                if target_id in state_by_target
                and state_by_target[target_id].claim_status in VERIFIED_CLAIMS
            }
            paths = []
            seen_paths = set()
            for path in paths_by_concept[concept_id]:
                key = (path["seriesId"], path["sectionId"])
                if key not in seen_paths:
                    seen_paths.add(key)
                    paths.append(path)
            nodes.append({
                **node,
                "recommendedTargetId": _recommended_target_id(
                    concept_target_ids,
                    failed_review_target_ids,
                ),
                "targetCount": len(concept_target_ids),
                "verifiedTargetCount": len(verified_ids),
                "required": bool(concept_target_ids & required_target_ids),
                "routeContexts": paths,
                "nextAction": _next_action(node),
            })

        included_ids = set(node_views)
        relation_rows = self.db.execute(
            select(ConceptRelationVersion)
            .join(
                KnowledgeGraphRelease,
                KnowledgeGraphRelease.id == ConceptRelationVersion.release_id,
            )
            .where(
                KnowledgeGraphRelease.status == "published",
                ConceptRelationVersion.status == "reviewed",
                ConceptRelationVersion.from_concept_revision_id.in_(included_ids),
                ConceptRelationVersion.to_concept_revision_id.in_(included_ids),
            )
            .order_by(
                ConceptRelationVersion.relation_type,
                ConceptRelationVersion.id,
            )
        ).scalars().all() if included_ids else []
        edges = [
            {
                "id": item.id,
                "from": item.from_concept_revision_id,
                "to": item.to_concept_revision_id,
                "type": item.relation_type,
                "label": RELATION_LABELS.get(item.relation_type, "相关"),
            }
            for item in relation_rows
        ]

        route_target_ids = required_target_ids & formal_target_ids
        verified_target_ids = {
            target_id
            for target_id in route_target_ids
            if target_id in state_by_target
            and state_by_target[target_id].claim_status in VERIFIED_CLAIMS
        }
        activation_counts = Counter(item["activation"] for item in nodes)
        rank_counts = Counter(item["rank"] for item in nodes)
        total = len(route_target_ids)
        availability = (
            "ready"
            if total and len(included_ids) == len(concept_ids)
            else "partial"
            if total
            else "not_ready"
        )
        return {
            "schemaVersion": "personal_knowledge_map_v1",
            "ruleVersion": KNOWLEDGE_MAP_RULE_VERSION,
            "rankRuleVersion": KNOWLEDGE_RANK_RULE_VERSION,
            "availability": availability,
            "scope": {
                "seriesId": series_id,
                "series": list(series_meta.values()),
                "definition": "当前目标中已发布并可验证的能力节点",
            },
            "progress": {
                "verifiedTargets": len(verified_target_ids),
                "requiredTargets": total,
                "coveragePpm": round(len(verified_target_ids) / total * 1_000_000)
                if total else 0,
                "activeNodes": activation_counts.get("active", 0),
                "needsWakeNodes": activation_counts.get("due", 0),
                "reassessmentNodes": activation_counts.get("reassessment", 0),
                "rankCounts": dict(sorted(rank_counts.items())),
                "basis": "latest_frozen_contracts_and_qualified_evidence",
            },
            "learnerProfile": learner_knowledge_profile_view(
                self.db,
                user_id=self.user_id,
            ),
            "nodes": nodes,
            "edges": edges,
            "excluded": {
                "provisionalTargetCount": len(targets) - len(formal_target_ids),
                "missingRubricNodeCount": len(concept_ids) - len(included_ids),
            },
            "message": (
                "这里显示的是正式目标与合格证据形成的个人知识子网。"
                if availability == "ready"
                else "部分目标仍在建立正式知识坐标，暂不参与段位与能力覆盖计算。"
                if availability == "partial"
                else "完成带有正式知识坐标的小节后，知识版图会从这里生长。"
            ),
        }

    @staticmethod
    def _empty(series_meta: dict[str, dict], *, series_id: str | None) -> dict:
        return {
            "schemaVersion": "personal_knowledge_map_v1",
            "ruleVersion": KNOWLEDGE_MAP_RULE_VERSION,
            "rankRuleVersion": KNOWLEDGE_RANK_RULE_VERSION,
            "availability": "not_ready",
            "scope": {
                "seriesId": series_id,
                "series": list(series_meta.values()),
                "definition": "当前目标中已发布并可验证的能力节点",
            },
            "progress": {
                "verifiedTargets": 0,
                "requiredTargets": 0,
                "coveragePpm": 0,
                "activeNodes": 0,
                "needsWakeNodes": 0,
                "reassessmentNodes": 0,
                "rankCounts": {},
                "basis": "latest_frozen_contracts_and_qualified_evidence",
            },
            "learnerProfile": {},
            "nodes": [],
            "edges": [],
            "excluded": {
                "provisionalTargetCount": 0,
                "missingRubricNodeCount": 0,
            },
            "message": "完成带有正式知识坐标的小节后，知识版图会从这里生长。",
        }
