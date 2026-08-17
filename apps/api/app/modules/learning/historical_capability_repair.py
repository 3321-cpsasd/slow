"""Audited capability-identity backfill for published historical lessons."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    Book,
    Chapter,
    ContentVersion,
    IdentityPublicationDecision,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    QuizSet,
    Section,
)
from .capabilities import (
    bind_assessment_target_to_capability_subnet,
    ensure_capability_route_binding,
    ensure_route_capability,
)


HISTORICAL_CAPABILITY_IDENTITY_RULE_VERSION = (
    "historical_capability_identity_v1"
)


def _dump(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _audit_binding(
    db: Session,
    *,
    contract_id: str,
    target: AssessmentTarget,
    series_id: str,
) -> bool:
    capability_id = str(target.capability_revision_id or "")
    criterion_id = str(target.capability_stage_criterion_id or "")
    decision_hash = _hash(
        HISTORICAL_CAPABILITY_IDENTITY_RULE_VERSION,
        contract_id,
        target.id,
        series_id,
        str(target.concept_revision_id or ""),
        capability_id,
        criterion_id,
    )
    existing = db.scalar(
        select(IdentityPublicationDecision).where(
            IdentityPublicationDecision.decision_hash == decision_hash
        )
    )
    if existing is not None:
        return False
    basis = {
        "mappingBasis": "exact_published_contract_target_and_unique_series",
        "sourceContractVersionId": contract_id,
        "sourceAssessmentTargetId": target.id,
        "seriesId": series_id,
        "conceptRevisionId": target.concept_revision_id,
        "capabilityRevisionId": capability_id,
        "capabilityStageCriterionId": criterion_id,
    }
    db.add(
        IdentityPublicationDecision(
            id=f"identity_publication_decision_{decision_hash[:32]}",
            subject_kind="target_capability_repair",
            candidate_id=f"{contract_id}:{target.id}",
            decision="approved_backfill",
            resolved_revision_id=capability_id,
            compared_revision_ids_json="[]",
            basis_json=_dump(basis),
            actor_kind="system_maintenance",
            actor_id="historical_capability_repair",
            rule_version=HISTORICAL_CAPABILITY_IDENTITY_RULE_VERSION,
            supersedes_id=None,
            decision_hash=decision_hash,
        )
    )
    db.flush()
    return True


def repair_published_historical_capability_identities(db: Session) -> dict:
    """Backfill exact route capabilities without rewriting published artifacts.

    The caller owns the transaction. The CLI runs this function in a rolled-back
    dry run unless ``--apply`` is explicitly supplied.
    """

    published = db.execute(
        select(
            QuizSet,
            ContentVersion,
            LearningContractVersion,
            Section,
            Book.series_id,
        )
        .join(ContentVersion, ContentVersion.id == QuizSet.content_version_id)
        .join(
            LearningContractVersion,
            LearningContractVersion.id == QuizSet.learning_contract_version_id,
        )
        .join(Section, Section.id == QuizSet.section_id)
        .join(Chapter, Chapter.id == Section.chapter_id)
        .join(Book, Book.id == Chapter.book_id)
        .where(
            QuizSet.publication_status == "published",
            ContentVersion.publication_status == "published",
            ContentVersion.learning_contract_version_id
            == QuizSet.learning_contract_version_id,
        )
        .order_by(QuizSet.id)
    ).all()

    observed_quiz_ids = set(
        db.scalars(
            select(AssessmentObservation.quiz_set_id)
            .where(AssessmentObservation.quiz_set_id.is_not(None))
            .distinct()
        ).all()
    )
    eligible = []
    skipped_quiz_ids: set[str] = set()
    for row in published:
        quiz = row[0]
        questions = _load(quiz.questions_json, [])
        target_ids = [
            str(question.get("assessmentTargetId") or "").strip()
            if isinstance(question, dict)
            else ""
            for question in questions
        ] if isinstance(questions, list) else []
        if not target_ids or any(not target_id for target_id in target_ids):
            if quiz.id in observed_quiz_ids:
                raise AppError(
                    "已有学习证据的历史题集缺少能力目标，修复已停止",
                    code="HISTORICAL_CAPABILITY_REPAIR_EVIDENCE_QUIZ_INVALID",
                    status=409,
                )
            skipped_quiz_ids.add(quiz.id)
            continue
        eligible.append(row)

    contract_ids = {contract.id for _, _, contract, _, _ in eligible}
    contract_bindings = {
        (item.contract_version_id, item.assessment_target_id)
        for item in db.scalars(
            select(LearningContractAssessmentTarget).where(
                LearningContractAssessmentTarget.contract_version_id.in_(
                    contract_ids
                ),
                LearningContractAssessmentTarget.diagnostic_only.is_(False),
            )
        ).all()
    } if contract_ids else set()
    target_ids = {
        str(question.get("assessmentTargetId") or "").strip()
        for quiz, _, _, _, _ in eligible
        for question in _load(quiz.questions_json, [])
        if isinstance(question, dict)
        and str(question.get("assessmentTargetId") or "").strip()
    }
    targets = {
        item.id: item
        for item in (
            db.scalars(
                select(AssessmentTarget).where(
                    AssessmentTarget.id.in_(target_ids)
                )
            ).all()
            if target_ids
            else []
        )
    }

    candidates: dict[tuple[str, str], dict] = {}
    series_by_target: defaultdict[str, set[str]] = defaultdict(set)
    for quiz, content, contract, section, series_id in eligible:
        if (
            quiz.section_id != contract.section_id
            or content.section_id != contract.section_id
        ):
            raise AppError(
                "历史题集与冻结契约不一致，修复已停止",
                code="HISTORICAL_CAPABILITY_REPAIR_CONTRACT_MISMATCH",
                status=409,
            )
        for question in _load(quiz.questions_json, []):
            target_id = str(question.get("assessmentTargetId") or "").strip()
            pair = (contract.id, target_id)
            target = targets.get(target_id)
            if target is None or pair not in contract_bindings:
                raise AppError(
                    "历史题目无法与冻结能力目标逐条对应，修复已停止",
                    code="HISTORICAL_CAPABILITY_REPAIR_TARGET_UNBOUND",
                    status=409,
                )
            if (
                not target.concept_revision_id
                or not target.learning_objective_id
                or target.status != "active"
            ):
                raise AppError(
                    "历史能力目标缺少可追溯的稳定知识身份，修复已停止",
                    code="HISTORICAL_CAPABILITY_REPAIR_IDENTITY_MISSING",
                    status=409,
                )
            candidates.setdefault(
                pair,
                {
                    "target": target,
                    "seriesId": series_id,
                    "sectionId": section.id,
                },
            )
            if candidates[pair]["seriesId"] != series_id:
                raise AppError(
                    "同一冻结能力目标跨越了不同学习路线，修复已停止",
                    code="HISTORICAL_CAPABILITY_REPAIR_ROUTE_CONFLICT",
                    status=409,
                )
            series_by_target[target_id].add(series_id)

    conflicting_targets = sorted(
        target_id
        for target_id, series_ids in series_by_target.items()
        if len(series_ids) != 1
    )
    if conflicting_targets:
        raise AppError(
            "历史能力目标同时属于多条学习路线，修复已停止",
            code="HISTORICAL_CAPABILITY_REPAIR_ROUTE_CONFLICT",
            status=409,
            details={"conflictingTargetCount": len(conflicting_targets)},
        )

    repaired_target_ids: set[str] = set()
    for target_id, series_ids in sorted(series_by_target.items()):
        target = targets[target_id]
        series_id = next(iter(series_ids))
        has_capability = bool(target.capability_revision_id)
        has_criterion = bool(target.capability_stage_criterion_id)
        if has_capability != has_criterion:
            raise AppError(
                "历史能力目标只有部分能力身份，修复已停止",
                code="HISTORICAL_CAPABILITY_REPAIR_PARTIAL_IDENTITY",
                status=409,
            )
        if not has_capability:
            capability, criterion = ensure_route_capability(
                db,
                series_id=series_id,
                concept_revision_id=str(target.concept_revision_id),
            )
            target.capability_revision_id = capability.id
            target.capability_stage_criterion_id = criterion.id
            repaired_target_ids.add(target.id)
            db.flush()
        else:
            ensure_capability_route_binding(
                db,
                series_id=series_id,
                capability_revision_id=str(target.capability_revision_id),
            )
        bind_assessment_target_to_capability_subnet(
            db,
            assessment_target_id=target.id,
            capability_revision_id=str(target.capability_revision_id),
            stage_criterion_id=str(target.capability_stage_criterion_id),
        )

    audit_decisions = 0
    for (contract_id, _target_id), candidate in sorted(candidates.items()):
        audit_decisions += int(
            _audit_binding(
                db,
                contract_id=contract_id,
                target=candidate["target"],
                series_id=candidate["seriesId"],
            )
        )

    historical_observation_count = int(
        len(
            db.scalars(
                select(AssessmentObservation).where(
                    AssessmentObservation.assessment_target_id.in_(
                        repaired_target_ids
                    )
                )
            ).all()
        )
        if repaired_target_ids
        else 0
    )
    db.flush()

    return {
        "ruleVersion": HISTORICAL_CAPABILITY_IDENTITY_RULE_VERSION,
        "publishedQuizSetsScanned": len(published),
        "eligibleQuizSets": len(eligible),
        "legacyQuizSetsSkipped": len(skipped_quiz_ids),
        "contractTargetPairs": len(candidates),
        "uniqueTargets": len(series_by_target),
        "targetsBackfilled": len(repaired_target_ids),
        "targetsAlreadyBound": len(series_by_target) - len(repaired_target_ids),
        "auditDecisionsCreated": audit_decisions,
        "historicalObservationsPreserved": historical_observation_count,
        "learnerProjectionsChanged": 0,
    }
