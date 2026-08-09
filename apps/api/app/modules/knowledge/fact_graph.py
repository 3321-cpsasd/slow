import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Chapter,
    Concept,
    ConceptObjectiveBinding,
    ConceptRelationVersion,
    ConceptRevision,
    ChapterCurriculumObjectiveBinding,
    CurriculumBaselineVersion,
    KnowledgeClaimBinding,
    KnowledgeGap,
    KnowledgeGapEvent,
    KnowledgeGraphRelease,
    KnowledgeSourceVersion,
    LearningObjective,
    SourceClaim,
    SourceClaimVersion,
)


KNOWLEDGE_GRAPH_RULE_VERSION = "knowledge_fact_graph_v1"
PUBLISHABLE_RIGHTS = frozenset({"public", "open_access", "licensed"})
VERIFIED_SUPPORT = frozenset({"verified", "cross_source"})
SUPPORTING_TYPES = frozenset({"supports", "defines"})


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class KnowledgeSourceInput(KnowledgeModel):
    source_key: str = Field(alias="sourceKey", min_length=1, max_length=160)
    source_kind: Literal[
        "official_documentation",
        "open_textbook",
        "open_course",
        "standard",
        "research_paper",
    ] = Field(alias="sourceKind")
    title: str = Field(min_length=1, max_length=500)
    authority: str = Field(min_length=1, max_length=240)
    url: HttpUrl
    version_label: str = Field(alias="versionLabel", min_length=1, max_length=200)
    retrieval_date: str = Field(alias="retrievalDate", min_length=10, max_length=32)
    content_digest: str = Field(alias="contentDigest", pattern=r"^[a-f0-9]{64}$")
    rights_status: Literal[
        "public", "open_access", "licensed", "metadata_only", "unknown"
    ] = Field(alias="rightsStatus")
    verification_status: Literal["candidate", "reviewed", "verified"] = Field(
        default="candidate", alias="verificationStatus"
    )
    provenance: dict = Field(default_factory=dict)


class ClaimSourceBindingInput(KnowledgeModel):
    source_key: str = Field(alias="sourceKey", min_length=1, max_length=160)
    locator_type: Literal["section", "heading", "page", "fragment", "paragraph"] = (
        Field(alias="locatorType")
    )
    locator: dict
    excerpt_hash: str = Field(default="", alias="excerptHash", pattern=r"^$|^[a-f0-9]{64}$")
    support_type: Literal["supports", "defines", "contradicts"] = Field(
        alias="supportType"
    )
    verification_status: Literal["candidate", "verified", "cross_source"] = Field(
        default="candidate", alias="verificationStatus"
    )
    review: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def locator_is_explicit(self):
        if not self.locator:
            raise ValueError("knowledge claim source locator cannot be empty")
        return self


class KnowledgeClaimInput(KnowledgeModel):
    key: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=8, max_length=2000)
    claim_kind: Literal[
        "definition", "mechanism", "boundary", "comparison", "application"
    ] = Field(alias="claimKind")
    concept_keys: list[str] = Field(
        default_factory=list, alias="conceptKeys", max_length=12
    )
    relation_keys: list[str] = Field(
        default_factory=list, alias="relationKeys", max_length=12
    )
    strict: bool = True
    source_bindings: list[ClaimSourceBindingInput] = Field(
        default_factory=list, alias="sourceBindings", max_length=12
    )

    @model_validator(mode="after")
    def has_subject(self):
        if not self.concept_keys and not self.relation_keys:
            raise ValueError("knowledge claim must support a concept or relation")
        return self


class KnowledgeConceptInput(KnowledgeModel):
    key: str = Field(min_length=1, max_length=160)
    revision: int = Field(default=1, ge=1)
    label: str = Field(min_length=1, max_length=300)
    definition: str = Field(min_length=8, max_length=3000)
    scope: dict = Field(default_factory=dict)
    boundaries: list[str] = Field(default_factory=list, max_length=30)
    objective_keys: list[str] = Field(
        alias="objectiveKeys", min_length=1, max_length=12
    )
    claim_keys: list[str] = Field(alias="claimKeys", min_length=1, max_length=20)


class KnowledgeRelationInput(KnowledgeModel):
    key: str = Field(min_length=1, max_length=160)
    from_concept_key: str = Field(alias="fromConceptKey", min_length=1, max_length=160)
    to_concept_key: str = Field(alias="toConceptKey", min_length=1, max_length=160)
    relation_type: Literal[
        "prerequisite_for", "applies_to", "contrasts_with", "refines", "part_of"
    ] = Field(alias="relationType")
    review_status: Literal["candidate", "reviewed"] = Field(
        default="candidate", alias="reviewStatus"
    )
    claim_keys: list[str] = Field(alias="claimKeys", min_length=1, max_length=12)
    provenance: dict = Field(default_factory=dict)


class DeclaredKnowledgeGapInput(KnowledgeModel):
    code: str = Field(min_length=1, max_length=100)
    severity: Literal["warning", "blocking"]
    subject_kind: Literal["concept", "concept_relation", "source_claim_version"] = (
        Field(alias="subjectKind")
    )
    subject_key: str = Field(alias="subjectKey", min_length=1, max_length=160)
    message: str = Field(min_length=8, max_length=1200)


class KnowledgeGraphSlicePackage(KnowledgeModel):
    schema_version: Literal["knowledge_graph_slice_v1"] = Field(
        alias="schemaVersion"
    )
    baseline_version_id: str = Field(alias="baselineVersionId", min_length=1)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    status: Literal["candidate"] = "candidate"
    sources: list[KnowledgeSourceInput] = Field(min_length=1, max_length=30)
    concepts: list[KnowledgeConceptInput] = Field(min_length=1, max_length=120)
    relations: list[KnowledgeRelationInput] = Field(min_length=1, max_length=600)
    claims: list[KnowledgeClaimInput] = Field(min_length=1, max_length=500)
    declared_gaps: list[DeclaredKnowledgeGapInput] = Field(
        default_factory=list, alias="declaredGaps", max_length=200
    )
    review_context: dict = Field(default_factory=dict, alias="reviewContext")

    @model_validator(mode="after")
    def references_are_closed(self):
        source_keys = [item.source_key for item in self.sources]
        concept_keys = [item.key for item in self.concepts]
        relation_keys = [item.key for item in self.relations]
        claim_keys = [item.key for item in self.claims]
        for label, keys in (
            ("source", source_keys),
            ("concept", concept_keys),
            ("relation", relation_keys),
            ("claim", claim_keys),
        ):
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} key")
        source_set = set(source_keys)
        concept_set = set(concept_keys)
        relation_set = set(relation_keys)
        claim_set = set(claim_keys)
        for item in self.concepts:
            if any(key not in claim_set for key in item.claim_keys):
                raise ValueError("concept references an unknown claim")
        for item in self.relations:
            if item.from_concept_key not in concept_set or item.to_concept_key not in concept_set:
                raise ValueError("relation references an unknown concept")
            if item.from_concept_key == item.to_concept_key:
                raise ValueError("relation cannot be self-referential")
            if any(key not in claim_set for key in item.claim_keys):
                raise ValueError("relation references an unknown claim")
        for item in self.claims:
            if any(key not in concept_set for key in item.concept_keys):
                raise ValueError("claim references an unknown concept")
            if any(key not in relation_set for key in item.relation_keys):
                raise ValueError("claim references an unknown relation")
            if any(binding.source_key not in source_set for binding in item.source_bindings):
                raise ValueError("claim binding references an unknown source")
        for item in self.declared_gaps:
            allowed_subjects = {
                "concept": concept_set,
                "concept_relation": relation_set,
                "source_claim_version": claim_set,
            }[item.subject_kind]
            if item.subject_key not in allowed_subjects:
                raise ValueError("declared gap references an unknown subject")
        return self

    def canonical_payload(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")

    def content_hash(self) -> str:
        return _hash(_json(self.canonical_payload()))


class KnowledgeGapDisposition(KnowledgeModel):
    gap_id: str = Field(alias="gapId", min_length=1)
    disposition: Literal["acknowledged_warning", "rejected"]
    rationale: str = Field(min_length=4, max_length=1000)


class KnowledgeGraphReviewManifest(KnowledgeModel):
    schema_version: Literal["knowledge_graph_review_v1"] = Field(
        alias="schemaVersion"
    )
    release_id: str = Field(alias="releaseId", min_length=1)
    content_hash: str = Field(alias="contentHash", pattern=r"^[a-f0-9]{64}$")
    decision: Literal["pending", "approved", "rejected"]
    reviewer_id: str = Field(alias="reviewerId", min_length=1, max_length=160)
    reviewed_at: str = Field(alias="reviewedAt", min_length=10, max_length=40)
    review_note: str = Field(alias="reviewNote", min_length=4, max_length=2000)
    accepted_source_keys: list[str] = Field(
        alias="acceptedSourceKeys", min_length=1, max_length=30
    )
    accepted_claim_keys: list[str] = Field(
        alias="acceptedClaimKeys", min_length=1, max_length=500
    )
    accepted_relation_keys: list[str] = Field(
        alias="acceptedRelationKeys", min_length=1, max_length=600
    )
    gap_dispositions: list[KnowledgeGapDisposition] = Field(
        default_factory=list, alias="gapDispositions", max_length=200
    )

    @model_validator(mode="after")
    def review_lists_are_sets(self):
        for label, values in (
            ("source", self.accepted_source_keys),
            ("claim", self.accepted_claim_keys),
            ("relation", self.accepted_relation_keys),
            ("gap", [item.gap_id for item in self.gap_dispositions]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate reviewed {label} key")
        return self


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _hash(*parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(material.encode()).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{_hash(*parts)[:32]}"


class KnowledgeFactGraphService:
    """Materialize reviewed facts, and expose them only through a published release."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def read_package(path: Path) -> KnowledgeGraphSlicePackage:
        return KnowledgeGraphSlicePackage.model_validate_json(path.read_text())

    @staticmethod
    def read_review(path: Path) -> KnowledgeGraphReviewManifest:
        return KnowledgeGraphReviewManifest.model_validate_json(path.read_text())

    def import_candidate(self, package: KnowledgeGraphSlicePackage) -> KnowledgeGraphRelease:
        baseline = self.db.get(CurriculumBaselineVersion, package.baseline_version_id)
        if not baseline:
            raise AppError(
                "课程基准不存在，不能建立知识事实切片",
                code="KNOWLEDGE_GRAPH_BASELINE_NOT_FOUND",
                status=404,
            )
        digest = package.content_hash()
        existing = self.db.scalar(
            select(KnowledgeGraphRelease).where(
                KnowledgeGraphRelease.baseline_version_id == baseline.id,
                KnowledgeGraphRelease.version == package.version,
            )
        )
        if existing:
            if existing.content_hash != digest:
                raise AppError(
                    "知识图谱发布版本已存在且内容不同，必须创建新版本",
                    code="KNOWLEDGE_GRAPH_VERSION_CONFLICT",
                    status=409,
                )
            return existing

        graph = json.loads(baseline.graph_json)
        baseline_concepts = {item["key"]: item for item in graph.get("concepts", [])}
        baseline_objectives = {item["key"]: item for item in graph.get("objectives", [])}
        unknown_concepts = sorted(
            item.key for item in package.concepts if item.key not in baseline_concepts
        )
        unknown_objectives = sorted(
            {
                key
                for item in package.concepts
                for key in item.objective_keys
                if key not in baseline_objectives
            }
        )
        if unknown_concepts or unknown_objectives:
            raise AppError(
                "知识切片引用了课程基准之外的概念或目标",
                code="KNOWLEDGE_GRAPH_BASELINE_SCOPE_VIOLATION",
                status=409,
                details={
                    "unknownConceptKeys": unknown_concepts,
                    "unknownObjectiveKeys": unknown_objectives,
                },
            )

        release = KnowledgeGraphRelease(
            id=_stable_id("knowledge_graph_release", baseline.id, package.version),
            baseline_version_id=baseline.id,
            version=package.version,
            status="candidate",
            manifest_json="{}",
            gaps_json="[]",
            content_hash=digest,
            review_json=_json({"candidateReviewContext": package.review_context}),
        )
        self.db.add(release)
        self.db.flush()

        source_rows = self._materialize_sources(package)
        objective_rows = self._materialize_objectives(
            baseline, package, baseline_objectives
        )
        concept_rows = self._materialize_concepts(baseline, package)
        objective_binding_rows = self._materialize_objective_bindings(
            release, package, concept_rows, objective_rows
        )
        claim_rows = self._materialize_claims(
            release, package, concept_rows, objective_rows
        )
        relation_rows = self._materialize_relations(
            release, package, concept_rows, claim_rows
        )
        claim_binding_rows = self._materialize_claim_bindings(
            release, package, claim_rows, source_rows
        )
        gap_rows = self._detect_gaps(
            release,
            package,
            source_rows,
            claim_rows,
            claim_binding_rows,
        )
        release.manifest_json = _json(
            {
                "title": package.title,
                "conceptRevisionIds": sorted(item.id for item in concept_rows.values()),
                "objectiveIds": sorted(item.id for item in objective_rows.values()),
                "relationVersionIds": sorted(item.id for item in relation_rows.values()),
                "claimVersionIds": sorted(item.id for item in claim_rows.values()),
                "sourceVersionIds": sorted(item.id for item in source_rows.values()),
                "claimBindingIds": sorted(item.id for item in claim_binding_rows),
                "conceptObjectiveBindingIds": sorted(
                    item.id for item in objective_binding_rows
                ),
                "ruleVersion": KNOWLEDGE_GRAPH_RULE_VERSION,
            }
        )
        release.gaps_json = _json(
            [
                {
                    "id": item.id,
                    "code": item.gap_type,
                    "severity": item.severity,
                    "subjectKind": item.subject_kind,
                }
                for item in gap_rows
            ]
        )
        self.db.commit()
        return release

    def publish(
        self,
        release_id: str,
        *,
        review: KnowledgeGraphReviewManifest,
    ) -> KnowledgeGraphRelease:
        release = self.db.get(KnowledgeGraphRelease, release_id)
        if not release:
            raise AppError(
                "知识图谱发布版本不存在",
                code="KNOWLEDGE_GRAPH_RELEASE_NOT_FOUND",
                status=404,
            )
        if release.status == "published":
            return release
        if review.decision != "approved":
            raise AppError(
                "知识图谱独立人工复核清单尚未批准",
                code="KNOWLEDGE_GRAPH_REVIEW_NOT_APPROVED",
                status=409,
            )
        if review.release_id != release.id or review.content_hash != release.content_hash:
            raise AppError(
                "知识图谱复核清单与候选发布版本不匹配",
                code="KNOWLEDGE_GRAPH_REVIEW_VERSION_MISMATCH",
                status=409,
            )
        baseline = self.db.get(CurriculumBaselineVersion, release.baseline_version_id)
        if not baseline or baseline.status != "published":
            raise AppError(
                "知识图谱只能绑定已发布课程基准",
                code="KNOWLEDGE_GRAPH_BASELINE_NOT_PUBLISHED",
                status=409,
            )
        gaps = json.loads(release.gaps_json or "[]")
        if any(item.get("severity") == "blocking" for item in gaps):
            raise AppError(
                "知识事实仍有阻断型来源缺口，不能发布",
                code="KNOWLEDGE_GRAPH_BLOCKING_GAP",
                status=409,
                details={"gaps": gaps},
            )

        manifest = json.loads(release.manifest_json)
        concepts = self._load_manifest_rows(
            ConceptRevision, manifest, "conceptRevisionIds"
        )
        objectives = self._load_manifest_rows(
            LearningObjective, manifest, "objectiveIds"
        )
        relations = self._load_manifest_rows(
            ConceptRelationVersion, manifest, "relationVersionIds"
        )
        claims = self._load_manifest_rows(
            SourceClaimVersion, manifest, "claimVersionIds"
        )
        sources = self._load_manifest_rows(
            KnowledgeSourceVersion, manifest, "sourceVersionIds"
        )
        bindings = self._load_manifest_rows(
            KnowledgeClaimBinding, manifest, "claimBindingIds"
        )
        source_keys = {item.source_key for item in sources}
        claim_keys = {
            json.loads(item.scope_json).get("claimKey", "") for item in claims
        }
        relation_keys = {
            json.loads(item.provenance_json).get("relationKey", "")
            for item in relations
        }
        gap_ids = {item.get("id", "") for item in gaps}
        if set(review.accepted_source_keys) != source_keys:
            raise AppError(
                "人工复核清单没有逐项覆盖全部知识来源",
                code="KNOWLEDGE_GRAPH_REVIEW_SOURCE_COVERAGE_MISMATCH",
                status=409,
            )
        if set(review.accepted_claim_keys) != claim_keys:
            raise AppError(
                "人工复核清单没有逐项覆盖全部知识主张",
                code="KNOWLEDGE_GRAPH_REVIEW_CLAIM_COVERAGE_MISMATCH",
                status=409,
            )
        if set(review.accepted_relation_keys) != relation_keys:
            raise AppError(
                "人工复核清单没有逐项覆盖全部知识关系",
                code="KNOWLEDGE_GRAPH_REVIEW_RELATION_COVERAGE_MISMATCH",
                status=409,
            )
        disposition_by_gap = {
            item.gap_id: item.disposition for item in review.gap_dispositions
        }
        if set(disposition_by_gap) != gap_ids or any(
            disposition_by_gap[item["id"]] != "acknowledged_warning"
            for item in gaps
            if item.get("severity") == "warning"
        ):
            raise AppError(
                "人工复核清单没有逐项处置全部知识缺口",
                code="KNOWLEDGE_GRAPH_REVIEW_GAP_COVERAGE_MISMATCH",
                status=409,
            )
        if any(item.status != "candidate" for item in relations):
            raise AppError(
                "知识关系候选状态异常",
                code="KNOWLEDGE_GRAPH_RELATION_STATE_INVALID",
                status=409,
            )
        if any(
            item.verification_status not in {"reviewed", "verified"}
            or item.rights_status not in PUBLISHABLE_RIGHTS
            for item in sources
        ):
            raise AppError(
                "知识来源尚未完成核验，或没有可用于知识事实的权利状态",
                code="KNOWLEDGE_GRAPH_SOURCE_NOT_PUBLISHABLE",
                status=409,
            )
        supported_claim_ids = {
            item.source_claim_version_id
            for item in bindings
            if item.support_type in SUPPORTING_TYPES
            and item.verification_status in VERIFIED_SUPPORT
            and bool(json.loads(item.locator_json))
        }
        missing_support = sorted(item.id for item in claims if item.id not in supported_claim_ids)
        if missing_support:
            raise AppError(
                "知识主张缺少已核验且可定位的来源支持",
                code="KNOWLEDGE_GRAPH_CLAIM_UNSUPPORTED",
                status=409,
                details={"claimVersionIds": missing_support},
            )

        reviewed_at = datetime.now(timezone.utc)
        for concept in concepts:
            concept.verification_status = "reviewed"
        for objective in objectives:
            objective.verification_status = "reviewed"
        for relation in relations:
            relation.status = "published"
        for source in sources:
            source.verification_status = "verified"
        for claim in claims:
            claim.trust_state = "verified"
            claim.status = "published"
        release.status = "published"
        release.review_json = _json(
            {
                **review.model_dump(by_alias=True, mode="json"),
                "reviewedAt": reviewed_at.isoformat(),
                "ruleVersion": KNOWLEDGE_GRAPH_RULE_VERSION,
            }
        )
        release.published_at = reviewed_at
        self.db.commit()
        return release

    def bind_chapter_identity_scope(
        self,
        *,
        chapter_id: str,
        baseline_version_id: str,
        objective_keys: list[str],
        concept_keys: list[str],
    ) -> dict:
        """Freeze exact graph identities selected during plan publication."""

        chapter = self.db.get(Chapter, chapter_id)
        if chapter is None:
            raise AppError(
                "章节不存在，不能绑定知识身份范围",
                code="CHAPTER_KNOWLEDGE_SCOPE_TARGET_MISSING",
                status=404,
            )
        release = self.db.scalar(
            select(KnowledgeGraphRelease)
            .where(
                KnowledgeGraphRelease.baseline_version_id == baseline_version_id,
                KnowledgeGraphRelease.status == "published",
            )
            .order_by(KnowledgeGraphRelease.version.desc())
        )
        if not release:
            if concept_keys:
                raise AppError(
                    "章节引用了尚未发布的知识概念",
                    code="CHAPTER_KNOWLEDGE_RELEASE_MISSING",
                    status=409,
                )
            scope = {
                "schemaVersion": "chapter_knowledge_identity_scope_v1",
                "baselineVersionId": baseline_version_id,
                "releaseId": "",
                "pairs": [],
            }
            chapter.knowledge_identity_scope_json = _json(scope)
            self.db.flush()
            return scope
        requested_concepts = set(concept_keys)
        requested_objectives = set(objective_keys)
        rows = self.db.execute(
            select(ConceptRevision, Concept, LearningObjective)
            .select_from(ConceptObjectiveBinding)
            .join(
                ConceptRevision,
                ConceptRevision.id == ConceptObjectiveBinding.concept_revision_id,
            )
            .join(Concept, Concept.id == ConceptRevision.concept_id)
            .join(
                LearningObjective,
                LearningObjective.id == ConceptObjectiveBinding.learning_objective_id,
            )
            .where(
                ConceptObjectiveBinding.release_id == release.id,
                Concept.concept_key.in_(requested_concepts),
                LearningObjective.objective_key.in_(requested_objectives),
                ConceptRevision.verification_status == "reviewed",
                LearningObjective.verification_status == "reviewed",
            )
            .order_by(Concept.concept_key, LearningObjective.objective_key)
        ).all() if requested_concepts and requested_objectives else []
        bound_concepts = {concept.concept_key for _, concept, _ in rows}
        missing = sorted(requested_concepts - bound_concepts)
        if missing:
            raise AppError(
                "章节知识概念与课程目标不属于同一已发布绑定",
                code="CHAPTER_KNOWLEDGE_IDENTITY_OUT_OF_SCOPE",
                status=409,
                details={"conceptKeys": missing},
            )
        scope = {
            "schemaVersion": "chapter_knowledge_identity_scope_v1",
            "baselineVersionId": baseline_version_id,
            "releaseId": release.id,
            "pairs": [
                {
                    "conceptRevisionId": revision.id,
                    "conceptKey": concept.concept_key,
                    "learningObjectiveId": objective.id,
                    "objectiveKey": objective.objective_key,
                }
                for revision, concept, objective in rows
            ],
        }
        chapter.knowledge_identity_scope_json = _json(scope)
        self.db.flush()
        return scope

    def chapter_identity_allowlist(self, chapter_id: str) -> list[dict]:
        """Return exact published concept/objective pairs for chapter planning."""

        chapter = self.db.get(Chapter, chapter_id)
        scope = _load_json(
            chapter.knowledge_identity_scope_json if chapter else "{}", {}
        )
        frozen_scope = (
            isinstance(scope, dict)
            and scope.get("schemaVersion") == "chapter_knowledge_identity_scope_v1"
        )
        frozen_pairs = scope.get("pairs", []) if frozen_scope else []
        if frozen_scope and not frozen_pairs:
            return []
        chapter_bindings = self.db.scalars(
            select(ChapterCurriculumObjectiveBinding).where(
                ChapterCurriculumObjectiveBinding.chapter_id == chapter_id
            )
        ).all()
        if not chapter_bindings:
            return []
        baseline_ids = {item.baseline_version_id for item in chapter_bindings}
        if len(baseline_ids) != 1:
            raise AppError(
                "章节必须恰好绑定一个课程基准版本",
                code="CHAPTER_KNOWLEDGE_BASELINE_BINDING_INVALID",
                status=409,
            )
        baseline_id = next(iter(baseline_ids))
        release = self.db.scalar(
            select(KnowledgeGraphRelease)
            .where(
                KnowledgeGraphRelease.baseline_version_id == baseline_id,
                KnowledgeGraphRelease.status == "published",
            )
            .order_by(KnowledgeGraphRelease.version.desc())
        )
        if not release:
            raise AppError(
                "章节课程基准尚无已发布知识图，不能生成小节",
                code="CHAPTER_KNOWLEDGE_RELEASE_MISSING",
                status=409,
            )
        if frozen_scope and scope.get("releaseId") != release.id:
            raise AppError(
                "章节冻结的知识图版本与当前发布版本不一致",
                code="CHAPTER_KNOWLEDGE_SCOPE_VERSION_MISMATCH",
                status=409,
            )
        objective_keys = {item.objective_key for item in chapter_bindings}
        frozen_pair_keys = {
            (str(item.get("conceptKey")), str(item.get("objectiveKey")))
            for item in frozen_pairs
            if isinstance(item, dict)
        }
        rows = self.db.execute(
            select(ConceptObjectiveBinding, ConceptRevision, Concept, LearningObjective)
            .join(
                ConceptRevision,
                ConceptRevision.id == ConceptObjectiveBinding.concept_revision_id,
            )
            .join(Concept, Concept.id == ConceptRevision.concept_id)
            .join(
                LearningObjective,
                LearningObjective.id == ConceptObjectiveBinding.learning_objective_id,
            )
            .where(
                ConceptObjectiveBinding.release_id == release.id,
                LearningObjective.objective_key.in_(objective_keys),
                ConceptRevision.verification_status == "reviewed",
                LearningObjective.verification_status == "reviewed",
            )
            .order_by(Concept.concept_key, LearningObjective.objective_key)
        ).all()
        if frozen_scope:
            rows = [
                row
                for row in rows
                if (row[2].concept_key, row[3].objective_key) in frozen_pair_keys
            ]
            actual_pairs = {
                (concept.concept_key, objective.objective_key)
                for _, _, concept, objective in rows
            }
            if actual_pairs != frozen_pair_keys:
                raise AppError(
                    "章节冻结的知识身份范围不完整或已损坏",
                    code="CHAPTER_KNOWLEDGE_SCOPE_INVALID",
                    status=409,
                )
        covered_objectives = {objective.objective_key for _, _, _, objective in rows}
        missing_objectives = sorted(objective_keys - covered_objectives)
        if not frozen_scope and missing_objectives:
            raise AppError(
                "章节目标尚未全部进入已发布知识图，不能生成小节",
                code="CHAPTER_KNOWLEDGE_OBJECTIVE_UNPUBLISHED",
                status=409,
                details={"objectiveKeys": missing_objectives},
            )
        return [
            {
                "releaseId": release.id,
                "conceptRevisionId": revision.id,
                "conceptKey": concept.concept_key,
                "conceptLabel": revision.label,
                "conceptDefinition": revision.definition,
                "conceptBoundaries": json.loads(revision.boundaries_json or "[]"),
                "objectiveId": objective.id,
                "objectiveKey": objective.objective_key,
                "objectiveStatement": objective.statement,
            }
            for _, revision, concept, objective in rows
        ]

    def validate_chapter_outline_identities(self, chapter_id: str, sections) -> list[dict]:
        allowlist = self.chapter_identity_allowlist(chapter_id)
        declared = [
            (item.baseline_concept_key, item.baseline_objective_key)
            for item in sections
            if item.baseline_concept_key or item.baseline_objective_key
        ]
        if not allowlist:
            if declared:
                raise AppError(
                    "非课程基准章节不能声明知识图身份",
                    code="CHAPTER_KNOWLEDGE_IDENTITY_NOT_APPLICABLE",
                    status=409,
                )
            return []
        if len(declared) != len(sections):
            raise AppError(
                "正式课程的每个小节都必须声明知识图概念和目标键",
                code="CHAPTER_KNOWLEDGE_IDENTITY_MISSING",
                status=502,
            )
        allowed_pairs = {
            (item["conceptKey"], item["objectiveKey"]) for item in allowlist
        }
        unknown = sorted(set(declared) - allowed_pairs)
        if unknown:
            raise AppError(
                "小节生成结果引用了章节允许清单之外的知识身份",
                code="CHAPTER_KNOWLEDGE_IDENTITY_OUT_OF_SCOPE",
                status=502,
                details={"pairs": [list(item) for item in unknown]},
            )
        required_concepts = {item["conceptKey"] for item in allowlist}
        covered_concepts = {concept_key for concept_key, _ in declared}
        missing_concepts = sorted(required_concepts - covered_concepts)
        if missing_concepts:
            raise AppError(
                "小节序列未覆盖章节已发布知识概念",
                code="CHAPTER_KNOWLEDGE_CONCEPT_COVERAGE_INCOMPLETE",
                status=502,
                details={"conceptKeys": missing_concepts},
            )
        return allowlist

    def _materialize_sources(
        self, package: KnowledgeGraphSlicePackage
    ) -> dict[str, KnowledgeSourceVersion]:
        result = {}
        for item in package.sources:
            row = self.db.scalar(
                select(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.source_key == item.source_key,
                    KnowledgeSourceVersion.version_label == item.version_label,
                )
            )
            if row and row.content_digest != item.content_digest:
                raise AppError(
                    "知识来源版本摘要冲突，必须创建新来源版本",
                    code="KNOWLEDGE_SOURCE_VERSION_CONFLICT",
                    status=409,
                )
            if not row:
                row = KnowledgeSourceVersion(
                    id=_stable_id("knowledge_source", item.source_key, item.version_label),
                    source_key=item.source_key,
                    source_kind=item.source_kind,
                    title=item.title,
                    authority=item.authority,
                    url=str(item.url),
                    version_label=item.version_label,
                    retrieval_date=item.retrieval_date,
                    content_digest=item.content_digest,
                    rights_status=item.rights_status,
                    verification_status=item.verification_status,
                    provenance_json=_json(item.provenance),
                )
                self.db.add(row)
                self.db.flush()
            result[item.source_key] = row
        return result

    def _materialize_objectives(
        self,
        baseline: CurriculumBaselineVersion,
        package: KnowledgeGraphSlicePackage,
        baseline_objectives: dict[str, dict],
    ) -> dict[str, LearningObjective]:
        result = {}
        objective_keys = sorted(
            {key for concept in package.concepts for key in concept.objective_keys}
        )
        for key in objective_keys:
            source = baseline_objectives[key]
            objective_id = _stable_id("learning_objective", baseline.baseline_key, key)
            row = self.db.get(LearningObjective, objective_id)
            if row and row.statement != source["statement"]:
                raise AppError(
                    "稳定学习目标身份已存在且语义不同",
                    code="KNOWLEDGE_OBJECTIVE_IDENTITY_CONFLICT",
                    status=409,
                )
            if not row:
                row = LearningObjective(
                    id=objective_id,
                    namespace=baseline.baseline_key,
                    objective_key=key,
                    statement=source["statement"],
                    cognitive_verb="demonstrate",
                    outcome_type=(
                        "practice"
                        if "code" in source.get("verificationPolicy", "")
                        else "knowledge"
                    ),
                    provenance_mode="published_curriculum_baseline",
                    verification_status="candidate",
                    status="active",
                )
                self.db.add(row)
                self.db.flush()
            result[key] = row
        return result

    def _materialize_concepts(
        self,
        baseline: CurriculumBaselineVersion,
        package: KnowledgeGraphSlicePackage,
    ) -> dict[str, ConceptRevision]:
        result = {}
        for item in package.concepts:
            concept_id = _stable_id("concept", baseline.baseline_key, item.key)
            concept = self.db.get(Concept, concept_id)
            if concept and (
                concept.namespace != baseline.baseline_key
                or concept.concept_key != item.key
            ):
                raise AppError(
                    "稳定概念身份发生冲突",
                    code="KNOWLEDGE_CONCEPT_IDENTITY_CONFLICT",
                    status=409,
                )
            if not concept:
                concept = Concept(
                    id=concept_id,
                    namespace=baseline.baseline_key,
                    concept_key=item.key,
                    canonical_name=item.label,
                    status="active",
                    origin="curriculum_knowledge_graph",
                )
                self.db.add(concept)
                self.db.flush()
            revision_id = _stable_id("concept_revision", concept.id, item.revision)
            revision = self.db.get(ConceptRevision, revision_id)
            expected_scope = _json(
                {
                    **item.scope,
                    "baselineVersionId": baseline.id,
                    "baselineConceptKey": item.key,
                }
            )
            expected_boundaries = _json(item.boundaries)
            if revision and (
                revision.label != item.label
                or revision.definition != item.definition
                or revision.scope_json != expected_scope
                or revision.boundaries_json != expected_boundaries
            ):
                raise AppError(
                    "概念修订号已存在且语义不同，必须递增修订号",
                    code="KNOWLEDGE_CONCEPT_REVISION_CONFLICT",
                    status=409,
                )
            if not revision:
                revision = ConceptRevision(
                    id=revision_id,
                    concept_id=concept.id,
                    revision=item.revision,
                    label=item.label,
                    definition=item.definition,
                    scope_json=expected_scope,
                    boundaries_json=expected_boundaries,
                    provenance_mode="source_review_candidate",
                    verification_status="candidate",
                )
                self.db.add(revision)
                self.db.flush()
            result[item.key] = revision
        return result

    def _materialize_objective_bindings(
        self,
        release: KnowledgeGraphRelease,
        package: KnowledgeGraphSlicePackage,
        concepts: dict[str, ConceptRevision],
        objectives: dict[str, LearningObjective],
    ) -> list[ConceptObjectiveBinding]:
        result = []
        for item in package.concepts:
            for objective_key in item.objective_keys:
                row = ConceptObjectiveBinding(
                    id=_stable_id(
                        "concept_objective_binding",
                        release.id,
                        concepts[item.key].id,
                        objectives[objective_key].id,
                    ),
                    release_id=release.id,
                    concept_revision_id=concepts[item.key].id,
                    learning_objective_id=objectives[objective_key].id,
                    binding_role="teaches",
                )
                self.db.add(row)
                result.append(row)
        self.db.flush()
        return result

    def _materialize_claims(
        self,
        release: KnowledgeGraphRelease,
        package: KnowledgeGraphSlicePackage,
        concepts: dict[str, ConceptRevision],
        objectives: dict[str, LearningObjective],
    ) -> dict[str, SourceClaimVersion]:
        result = {}
        concept_inputs = {item.key: item for item in package.concepts}
        for item in package.claims:
            stable_key = _hash(release.baseline_version_id, item.key)
            claim = self.db.scalar(
                select(SourceClaim).where(SourceClaim.stable_key == stable_key)
            )
            if not claim:
                claim = SourceClaim(
                    id=_stable_id("knowledge_claim", stable_key),
                    stable_key=stable_key,
                    status="active",
                )
                self.db.add(claim)
                self.db.flush()
            claim_version_id = _stable_id(
                "knowledge_claim_version", claim.id, release.version
            )
            claim_version = self.db.get(SourceClaimVersion, claim_version_id)
            scope_json = _json(
                {
                    "baselineVersionId": release.baseline_version_id,
                    "claimKey": item.key,
                    "conceptKeys": item.concept_keys,
                    "relationKeys": item.relation_keys,
                    "conceptRevisionIds": [
                        concepts[key].id for key in item.concept_keys
                    ],
                    "learningObjectiveIds": sorted(
                        {
                            objectives[objective_key].id
                            for concept_key in item.concept_keys
                            for objective_key in concept_inputs[
                                concept_key
                            ].objective_keys
                        }
                    ),
                    "relationVersionIds": [
                        _stable_id("concept_relation", release.id, key)
                        for key in item.relation_keys
                    ],
                }
            )
            if claim_version and (
                claim_version.statement != item.statement
                or claim_version.claim_kind != item.claim_kind
                or claim_version.scope_json != scope_json
            ):
                raise AppError(
                    "知识主张版本已存在且内容不同",
                    code="KNOWLEDGE_CLAIM_VERSION_CONFLICT",
                    status=409,
                )
            if not claim_version:
                claim_version = SourceClaimVersion(
                    id=claim_version_id,
                    source_claim_id=claim.id,
                    version=release.version,
                    statement=item.statement,
                    claim_kind=item.claim_kind,
                    scope_json=scope_json,
                    strict=item.strict,
                    trust_state="unverified",
                    generation_method="source_extraction_candidate",
                    status="candidate",
                )
                self.db.add(claim_version)
                self.db.flush()
            result[item.key] = claim_version
        return result

    def _materialize_relations(
        self,
        release: KnowledgeGraphRelease,
        package: KnowledgeGraphSlicePackage,
        concepts: dict[str, ConceptRevision],
        claims: dict[str, SourceClaimVersion],
    ) -> dict[str, ConceptRelationVersion]:
        result = {}
        for item in package.relations:
            row = ConceptRelationVersion(
                id=_stable_id("concept_relation", release.id, item.key),
                release_id=release.id,
                from_concept_revision_id=concepts[item.from_concept_key].id,
                to_concept_revision_id=concepts[item.to_concept_key].id,
                relation_type=item.relation_type,
                relation_revision=1,
                status="candidate",
                provenance_json=_json(
                    {
                        **item.provenance,
                        "relationKey": item.key,
                        "reviewStatus": item.review_status,
                        "claimVersionIds": [claims[key].id for key in item.claim_keys],
                    }
                ),
            )
            self.db.add(row)
            result[item.key] = row
        self.db.flush()
        return result

    def _materialize_claim_bindings(
        self,
        release: KnowledgeGraphRelease,
        package: KnowledgeGraphSlicePackage,
        claims: dict[str, SourceClaimVersion],
        sources: dict[str, KnowledgeSourceVersion],
    ) -> list[KnowledgeClaimBinding]:
        result = []
        for item in package.claims:
            for binding in item.source_bindings:
                locator_json = _json(binding.locator)
                locator_hash = _hash(binding.locator_type, locator_json)
                row = KnowledgeClaimBinding(
                    id=_stable_id(
                        "knowledge_claim_binding",
                        release.id,
                        claims[item.key].id,
                        sources[binding.source_key].id,
                        locator_hash,
                    ),
                    release_id=release.id,
                    source_claim_version_id=claims[item.key].id,
                    knowledge_source_version_id=sources[binding.source_key].id,
                    locator_type=binding.locator_type,
                    locator_json=locator_json,
                    locator_hash=locator_hash,
                    excerpt_hash=binding.excerpt_hash,
                    support_type=binding.support_type,
                    verification_status=binding.verification_status,
                    review_json=_json(binding.review),
                )
                self.db.add(row)
                result.append(row)
        self.db.flush()
        return result

    def _detect_gaps(
        self,
        release: KnowledgeGraphRelease,
        package: KnowledgeGraphSlicePackage,
        sources: dict[str, KnowledgeSourceVersion],
        claims: dict[str, SourceClaimVersion],
        bindings: list[KnowledgeClaimBinding],
    ) -> list[KnowledgeGap]:
        result = []
        verified_claim_ids = {
            item.source_claim_version_id
            for item in bindings
            if item.support_type in SUPPORTING_TYPES
            and item.verification_status in VERIFIED_SUPPORT
            and sources_by_id(sources)[item.knowledge_source_version_id].verification_status
            == "reviewed"
            and sources_by_id(sources)[item.knowledge_source_version_id].rights_status
            in PUBLISHABLE_RIGHTS
        }
        for item in package.claims:
            claim = claims[item.key]
            if claim.id not in verified_claim_ids:
                result.append(
                    self._create_gap(
                        release,
                        gap_type="KNOWLEDGE_CLAIM_VERIFIED_SOURCE_MISSING",
                        subject_kind="source_claim_version",
                        subject_key=item.key,
                        source_claim_version_id=claim.id,
                        details={
                            "claimKey": item.key,
                            "required": "reviewed publishable source with verified locator binding",
                        },
                    )
                )
        claims_by_key = set(claims)
        for concept in package.concepts:
            if not set(concept.claim_keys) & claims_by_key:
                result.append(
                    self._create_gap(
                        release,
                        gap_type="KNOWLEDGE_CONCEPT_CLAIM_MISSING",
                        subject_kind="concept",
                        subject_key=concept.key,
                        details={"conceptKey": concept.key},
                    )
                )
        for relation in package.relations:
            if not set(relation.claim_keys) & claims_by_key:
                result.append(
                    self._create_gap(
                        release,
                        gap_type="KNOWLEDGE_RELATION_CLAIM_MISSING",
                        subject_kind="concept_relation",
                        subject_key=relation.key,
                        details={"relationKey": relation.key},
                    )
                )
        for item in package.declared_gaps:
            result.append(
                self._create_gap(
                    release,
                    gap_type=item.code,
                    severity=item.severity,
                    subject_kind=item.subject_kind,
                    subject_key=item.subject_key,
                    source_claim_version_id=(
                        claims[item.subject_key].id
                        if item.subject_kind == "source_claim_version"
                        else None
                    ),
                    details={
                        "subjectKey": item.subject_key,
                        "message": item.message,
                        "declared": True,
                    },
                )
            )
        return result

    def _create_gap(
        self,
        release: KnowledgeGraphRelease,
        *,
        gap_type: str,
        severity: str = "blocking",
        subject_kind: str,
        subject_key: str,
        details: dict,
        source_claim_version_id: str | None = None,
    ) -> KnowledgeGap:
        gap = KnowledgeGap(
            id=_stable_id("knowledge_graph_gap", release.id, gap_type, subject_key),
            gap_type=gap_type,
            severity=severity,
            subject_kind=subject_kind,
            source_claim_version_id=source_claim_version_id,
            content_version_id=None,
            content_block_version_id=None,
            detector_kind="deterministic_rule",
            detector_rule_version=KNOWLEDGE_GRAPH_RULE_VERSION,
            details_json=_json({**details, "releaseId": release.id}),
        )
        self.db.add(gap)
        self.db.flush()
        self.db.add(
            KnowledgeGapEvent(
                id=_stable_id("knowledge_graph_gap_event", gap.id, "opened"),
                knowledge_gap_id=gap.id,
                event_type="opened",
                actor_kind="system_rule",
                actor_id="",
                rationale="knowledge fact graph publication support is incomplete",
                evidence_json=_json({"releaseId": release.id}),
                rule_version=KNOWLEDGE_GRAPH_RULE_VERSION,
                idempotency_key=f"opened:{gap.id}",
            )
        )
        self.db.flush()
        return gap

    def _load_manifest_rows(self, model, manifest: dict, key: str):
        ids = manifest.get(key, [])
        rows = [self.db.get(model, item_id) for item_id in ids]
        if not ids or any(item is None for item in rows):
            raise AppError(
                "知识图谱成员清单不完整或已损坏",
                code="KNOWLEDGE_GRAPH_MANIFEST_INVALID",
                status=409,
                details={"manifestKey": key},
            )
        return rows


def sources_by_id(
    sources: dict[str, KnowledgeSourceVersion],
) -> dict[str, KnowledgeSourceVersion]:
    return {item.id: item for item in sources.values()}


@dataclass(frozen=True)
class PublishedKnowledgeIdentity:
    release_id: str
    concept_revision_id: str
    concept_key: str
    concept_label: str
    learning_objective_id: str
    objective_key: str
    objective_statement: str
    verification_policy: str


def resolve_published_section_identities(
    db: Session,
    *,
    chapter_id: str,
    objectives_json: str,
) -> list[PublishedKnowledgeIdentity]:
    """Resolve explicit section keys; never guess from prose, title, or position."""

    try:
        raw_items = json.loads(objectives_json or "[]")
    except (TypeError, ValueError):
        raw_items = []
    references: list[tuple[str, str]] = []
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        concept_key = str(item.get("baselineConceptKey") or "").strip()
        objective_key = str(item.get("baselineObjectiveKey") or "").strip()
        if concept_key or objective_key:
            if not concept_key or not objective_key:
                raise AppError(
                    "小节知识身份必须同时声明基准概念键和目标键",
                    code="SECTION_KNOWLEDGE_IDENTITY_INCOMPLETE",
                    status=409,
                )
            pair = (concept_key, objective_key)
            if pair not in references:
                references.append(pair)
    if not references:
        return []

    chapter = db.get(Chapter, chapter_id)
    scope = _load_json(
        chapter.knowledge_identity_scope_json if chapter else "{}", {}
    )
    if (
        isinstance(scope, dict)
        and scope.get("schemaVersion") == "chapter_knowledge_identity_scope_v1"
    ):
        allowed_pairs = {
            (str(item.get("conceptKey")), str(item.get("objectiveKey")))
            for item in scope.get("pairs", [])
            if isinstance(item, dict)
        }
        outside = sorted(set(references) - allowed_pairs)
        if outside:
            raise AppError(
                "小节知识身份超出章节冻结的概念范围",
                code="SECTION_KNOWLEDGE_IDENTITY_OUT_OF_CHAPTER_SCOPE",
                status=409,
                details={"pairs": [list(item) for item in outside]},
            )

    chapter_bindings = db.scalars(
        select(ChapterCurriculumObjectiveBinding).where(
            ChapterCurriculumObjectiveBinding.chapter_id == chapter_id
        )
    ).all()
    baseline_ids = {item.baseline_version_id for item in chapter_bindings}
    bound_objectives = {item.objective_key for item in chapter_bindings}
    if len(baseline_ids) != 1:
        raise AppError(
            "小节显式知识身份要求章节恰好绑定一个课程基准版本",
            code="SECTION_KNOWLEDGE_BASELINE_BINDING_INVALID",
            status=409,
        )
    unknown_objectives = sorted(
        objective_key
        for _, objective_key in references
        if objective_key not in bound_objectives
    )
    if unknown_objectives:
        raise AppError(
            "小节引用了章节未覆盖的课程目标",
            code="SECTION_KNOWLEDGE_OBJECTIVE_OUT_OF_SCOPE",
            status=409,
            details={"objectiveKeys": unknown_objectives},
        )
    baseline_id = next(iter(baseline_ids))
    release = db.scalar(
        select(KnowledgeGraphRelease)
        .where(
            KnowledgeGraphRelease.baseline_version_id == baseline_id,
            KnowledgeGraphRelease.status == "published",
        )
        .order_by(KnowledgeGraphRelease.version.desc())
    )
    if not release:
        raise AppError(
            "小节引用的课程基准尚无已发布知识图谱版本",
            code="SECTION_KNOWLEDGE_RELEASE_MISSING",
            status=409,
        )
    if scope.get("releaseId") and scope.get("releaseId") != release.id:
        raise AppError(
            "小节引用的知识图版本与章节冻结范围不一致",
            code="SECTION_KNOWLEDGE_SCOPE_VERSION_MISMATCH",
            status=409,
        )
    baseline = db.get(CurriculumBaselineVersion, baseline_id)
    graph = json.loads(baseline.graph_json) if baseline else {}
    policy_by_objective = {
        item["key"]: item.get("verificationPolicy", "choice_quiz_v1")
        for item in graph.get("objectives", [])
    }
    result = []
    for concept_key, objective_key in references:
        row = db.execute(
            select(
                ConceptObjectiveBinding,
                ConceptRevision,
                Concept,
                LearningObjective,
            )
            .join(
                ConceptRevision,
                ConceptRevision.id == ConceptObjectiveBinding.concept_revision_id,
            )
            .join(Concept, Concept.id == ConceptRevision.concept_id)
            .join(
                LearningObjective,
                LearningObjective.id == ConceptObjectiveBinding.learning_objective_id,
            )
            .where(
                ConceptObjectiveBinding.release_id == release.id,
                Concept.concept_key == concept_key,
                LearningObjective.objective_key == objective_key,
                ConceptRevision.verification_status == "reviewed",
                LearningObjective.verification_status == "reviewed",
            )
        ).one_or_none()
        if not row:
            raise AppError(
                "小节知识身份不属于已发布知识图谱，或尚未完成复核",
                code="SECTION_KNOWLEDGE_IDENTITY_UNPUBLISHED",
                status=409,
                details={
                    "conceptKey": concept_key,
                    "objectiveKey": objective_key,
                    "releaseId": release.id,
                },
            )
        _, revision, concept, objective = row
        result.append(
            PublishedKnowledgeIdentity(
                release_id=release.id,
                concept_revision_id=revision.id,
                concept_key=concept.concept_key,
                concept_label=revision.label,
                learning_objective_id=objective.id,
                objective_key=objective.objective_key,
                objective_statement=objective.statement,
                verification_policy=policy_by_objective.get(
                    objective.objective_key, "choice_quiz_v1"
                ),
            )
        )
    return result
