"""Optional reviewed standard packages for routes needing offline continuity.

Packages are bound by an exact deterministic contract signature. A match never
bypasses the normal lesson candidate validator or atomic publisher. M3's
default recoverable route does not require this repository: model failure may
be surfaced explicitly and retried after provider recovery.
"""

from __future__ import annotations

from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.contracts import GeneratedLessonCandidate
from ..core.errors import AppError
from ..infrastructure.tables import (
    Book,
    Chapter,
    LearningContractVersion,
    RouteAdmissionDecision,
    Section,
    SectionFallbackBinding,
    Series,
    StandardLessonPackageTarget,
    StandardLessonPackageVersion,
    now,
)
from .lesson_generation import LessonGenerationSpec, validate_lesson_candidate


STANDARD_PACKAGE_SCHEMA_VERSION = "standard_lesson_package_v1"
STANDARD_PACKAGE_RULE_VERSION = "standard_package_exact_contract_v1"
ROUTE_ADMISSION_RULE_VERSION = "guaranteed_route_admission_v1"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _hash(value) -> str:
    return sha256(_dump(value).encode("utf-8")).hexdigest()


def contract_snapshot(spec: LessonGenerationSpec) -> dict:
    """Return only frozen semantic and applicability fields used for matching."""

    return {
        "language": "zh-CN",
        "sectionQuestion": str(spec.section.get("question") or ""),
        "targetDepth": spec.depth_policy,
        "targets": [
            {
                "assessmentTargetId": item.assessment_target_id,
                "conceptRevisionId": item.concept_revision_id,
                "dimension": item.dimension,
                "targetDepth": item.target_depth,
                "required": item.required,
                "verificationPolicy": item.verification_policy,
            }
            for item in spec.targets
        ],
        "neighborBoundaries": [
            item.model_dump(by_alias=True) for item in spec.neighbor_boundaries
        ],
        "knowledgeContext": {
            "status": spec.knowledge_context.get("status"),
            "releaseId": spec.knowledge_context.get("releaseId"),
            "nodeRevisionIds": sorted(
                str(item.get("conceptRevisionId") or "")
                for item in spec.knowledge_context.get("nodes", [])
            ),
            "claimVersionIds": sorted(
                str(item.get("claimVersionId") or "")
                for item in spec.knowledge_context.get("claims", [])
            ),
        },
        "compositionPolicy": spec.composition_policy.model_dump(by_alias=True),
    }


def contract_signature(spec: LessonGenerationSpec) -> str:
    return _hash(contract_snapshot(spec))


class StandardContentService:
    def __init__(self, db: Session):
        self.db = db

    def publish_package(
        self,
        *,
        package_key: str,
        version: int,
        title: str,
        spec: LessonGenerationSpec,
        candidate: GeneratedLessonCandidate,
        review: dict,
    ) -> StandardLessonPackageVersion:
        if review.get("status") != "approved":
            raise AppError(
                "标准内容包必须先完成人工审核",
                code="STANDARD_PACKAGE_REVIEW_REQUIRED",
                status=409,
            )
        validate_lesson_candidate(spec, candidate)
        snapshot = contract_snapshot(spec)
        payload = candidate.model_dump(by_alias=True)
        output_hash = _hash({"contract": snapshot, "candidate": payload})
        package = StandardLessonPackageVersion(
            id=f"standard_package_{uuid4().hex}",
            package_key=package_key,
            version=version,
            title=title,
            contract_signature=_hash(snapshot),
            contract_snapshot_json=_dump(snapshot),
            composition_policy_json=_dump(
                spec.composition_policy.model_dump(by_alias=True)
            ),
            blocks_json=_dump(payload["blocks"]),
            questions_json=_dump(payload["questions"]),
            sources_json="[]",
            status="published",
            review_status="approved",
            rights_status="reviewed",
            factual_status="reviewed",
            schema_version=STANDARD_PACKAGE_SCHEMA_VERSION,
            rule_version=STANDARD_PACKAGE_RULE_VERSION,
            output_hash=output_hash,
            review_json=_dump(review),
            published_at=now(),
        )
        self.db.add(package)
        self.db.flush()
        for position, target in enumerate(spec.targets):
            self.db.add(StandardLessonPackageTarget(
                id=f"standard_package_target_{uuid4().hex}",
                package_version_id=package.id,
                assessment_target_id=target.assessment_target_id,
                position=position,
                required=target.required,
                verification_policy=target.verification_policy,
                target_depth=target.target_depth,
            ))
        self.db.flush()
        return package

    def bind(
        self,
        *,
        section: Section,
        contract: LearningContractVersion,
        spec: LessonGenerationSpec,
        package: StandardLessonPackageVersion,
    ) -> SectionFallbackBinding:
        signature = contract_signature(spec)
        if (
            contract.section_id != section.id
            or package.status != "published"
            or package.review_status != "approved"
            or package.rights_status != "reviewed"
            or package.factual_status != "reviewed"
            or package.rule_version != STANDARD_PACKAGE_RULE_VERSION
            or package.contract_signature != signature
            or _load(package.contract_snapshot_json, {}) != contract_snapshot(spec)
        ):
            raise AppError(
                "标准内容包与当前学习契约不匹配",
                code="STANDARD_PACKAGE_CONTRACT_MISMATCH",
                status=409,
            )
        candidate = self._candidate(package)
        validate_lesson_candidate(spec, candidate)
        existing = self.db.scalar(select(SectionFallbackBinding).where(
            SectionFallbackBinding.learning_contract_version_id == contract.id
        ))
        if existing:
            if existing.standard_package_version_id != package.id:
                raise AppError(
                    "当前契约已经绑定其他标准内容版本",
                    code="STANDARD_PACKAGE_BINDING_CONFLICT",
                    status=409,
                )
            return existing
        binding = SectionFallbackBinding(
            id=f"section_fallback_{uuid4().hex}",
            section_id=section.id,
            learning_contract_version_id=contract.id,
            standard_package_version_id=package.id,
            contract_signature=signature,
            status="active",
        )
        self.db.add(binding)
        self.db.flush()
        return binding

    def fallback_candidate(
        self,
        *,
        contract: LearningContractVersion,
        spec: LessonGenerationSpec,
    ) -> tuple[GeneratedLessonCandidate, StandardLessonPackageVersion]:
        binding = self.db.scalar(select(SectionFallbackBinding).where(
            SectionFallbackBinding.learning_contract_version_id == contract.id,
            SectionFallbackBinding.status == "active",
        ))
        if not binding or binding.contract_signature != contract_signature(spec):
            raise AppError(
                "正式路线缺少与当前契约精确匹配的标准内容",
                code="GUARANTEED_ROUTE_FALLBACK_MISSING",
                status=503,
                retryable=True,
            )
        package = self.db.get(
            StandardLessonPackageVersion, binding.standard_package_version_id
        )
        if not package or package.contract_signature != binding.contract_signature:
            raise AppError(
                "正式路线的标准内容绑定已失效",
                code="GUARANTEED_ROUTE_FALLBACK_INVALID",
                status=503,
                retryable=True,
            )
        candidate = self._candidate(package)
        validate_lesson_candidate(spec, candidate)
        return candidate, package

    def admit_series(self, series: Series) -> RouteAdmissionDecision:
        section_ids = list(self.db.scalars(
            select(Section.id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .where(Book.series_id == series.id)
        ))
        latest_contracts: list[LearningContractVersion] = []
        for section_id in section_ids:
            contract = self.db.scalar(
                select(LearningContractVersion)
                .where(LearningContractVersion.section_id == section_id)
                .order_by(LearningContractVersion.version.desc())
            )
            if contract:
                latest_contracts.append(contract)
        contract_ids = [item.id for item in latest_contracts]
        bindings = list(self.db.scalars(
            select(SectionFallbackBinding).where(
                SectionFallbackBinding.learning_contract_version_id.in_(contract_ids),
                SectionFallbackBinding.status == "active",
            )
        )) if contract_ids else []
        covered_ids = {item.learning_contract_version_id for item in bindings}
        reasons = []
        if len(latest_contracts) != len(section_ids):
            reasons.append("contract_missing")
        if len(covered_ids) != len(latest_contracts):
            reasons.append("fallback_binding_missing")
        allowed = bool(section_ids) and not reasons
        decision_input = {
            "seriesId": series.id,
            "sectionIds": sorted(section_ids),
            "contractIds": sorted(contract_ids),
            "coveredContractIds": sorted(covered_ids),
            "ruleVersion": ROUTE_ADMISSION_RULE_VERSION,
        }
        decision = RouteAdmissionDecision(
            id=f"route_admission_{uuid4().hex}",
            series_id=series.id,
            allowed=allowed,
            covered_contracts=len(covered_ids),
            required_contracts=len(section_ids),
            reasons_json=_dump(reasons),
            rule_version=ROUTE_ADMISSION_RULE_VERSION,
            input_hash=_hash(decision_input),
        )
        self.db.add(decision)
        if allowed:
            series.continuity_tier = "guaranteed"
        self.db.flush()
        return decision

    @staticmethod
    def _candidate(package: StandardLessonPackageVersion) -> GeneratedLessonCandidate:
        if (
            package.status != "published"
            or package.review_status != "approved"
            or package.rights_status != "reviewed"
            or package.factual_status != "reviewed"
            or package.schema_version != STANDARD_PACKAGE_SCHEMA_VERSION
            or package.rule_version != STANDARD_PACKAGE_RULE_VERSION
        ):
            raise AppError(
                "标准内容包未达到正式发布条件",
                code="STANDARD_PACKAGE_NOT_ELIGIBLE",
                status=409,
            )
        return GeneratedLessonCandidate.model_validate({
            "decision": "candidate",
            "confidence": "high",
            "blocks": _load(package.blocks_json, []),
            "questions": _load(package.questions_json, []),
        })
