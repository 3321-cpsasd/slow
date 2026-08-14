"""Audited, append-only rank identity repair for published legacy lessons."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    AssessmentTargetRankIdentityDecision,
    Book,
    Chapter,
    ContentVersion,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    QuizAttempt,
    QuizSet,
    Section,
)
from .assessment import rebuild_assessment_projections
from .contracts import materialize_route_target
from .knowledge_ranks import resolve_effective_rank_target


HISTORICAL_RANK_IDENTITY_RULE_VERSION = "historical_rank_identity_v1"


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


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _latest_decision(
    db: Session,
    *,
    contract_id: str,
    target_id: str,
) -> AssessmentTargetRankIdentityDecision | None:
    return db.scalar(
        select(AssessmentTargetRankIdentityDecision)
        .where(
            AssessmentTargetRankIdentityDecision.source_contract_version_id
            == contract_id,
            AssessmentTargetRankIdentityDecision.source_assessment_target_id
            == target_id,
        )
        .order_by(
            AssessmentTargetRankIdentityDecision.created_at.desc(),
            AssessmentTargetRankIdentityDecision.id.desc(),
        )
    )


def repair_published_historical_rank_identities(db: Session) -> dict:
    """Bridge exact frozen legacy targets, then replay their affected learners.

    The caller owns the transaction. A dry run can call this function and roll
    the session back; production maintenance commits only after the full repair
    and every learner projection rebuild succeeds.
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
    eligible_published = []
    skipped_quiz_ids: set[str] = set()
    for row in published:
        quiz = row[0]
        questions = _load(quiz.questions_json, [])
        target_ids_for_quiz = [
            str(question.get("assessmentTargetId") or "").strip()
            if isinstance(question, dict)
            else ""
            for question in questions
        ] if isinstance(questions, list) else []
        if (
            not target_ids_for_quiz
            or any(not target_id for target_id in target_ids_for_quiz)
        ):
            if quiz.id in observed_quiz_ids:
                raise AppError(
                    "已有学习证据的历史题集缺少能力目标，修复已停止",
                    code="HISTORICAL_RANK_REPAIR_EVIDENCE_QUIZ_INVALID",
                    status=409,
                )
            skipped_quiz_ids.add(quiz.id)
            continue
        eligible_published.append(row)

    contract_ids = {
        contract.id for _, _, contract, _, _ in eligible_published
    }
    bindings = {
        (item.contract_version_id, item.assessment_target_id)
        for item in db.scalars(
            select(LearningContractAssessmentTarget).where(
                LearningContractAssessmentTarget.contract_version_id.in_(
                    contract_ids
                ),
                LearningContractAssessmentTarget.diagnostic_only.is_(False),
            )
        ).all()
    }
    target_ids = {
        str(question.get("assessmentTargetId") or "").strip()
        for quiz, _, _, _, _ in eligible_published
        for question in _load(quiz.questions_json, [])
        if isinstance(question, dict)
        and str(question.get("assessmentTargetId") or "").strip()
    }
    targets = {
        item.id: item
        for item in db.scalars(
            select(AssessmentTarget).where(AssessmentTarget.id.in_(target_ids))
        ).all()
    }

    candidates: dict[tuple[str, str], dict] = {}
    direct_pairs: set[tuple[str, str]] = set()
    for quiz, content, contract, section, series_id in eligible_published:
        if (
            quiz.section_id != contract.section_id
            or content.section_id != contract.section_id
        ):
            raise AppError(
                "历史题集与冻结契约不一致，修复已停止",
                code="HISTORICAL_RANK_REPAIR_CONTRACT_MISMATCH",
                status=409,
            )
        questions = _load(quiz.questions_json, [])
        for question in questions:
            target_id = (
                str(question.get("assessmentTargetId") or "").strip()
                if isinstance(question, dict)
                else ""
            )
            source = targets.get(target_id)
            pair = (contract.id, target_id)
            if not target_id or source is None or pair not in bindings:
                raise AppError(
                    "历史题目无法与冻结能力目标逐条对应，修复已停止",
                    code="HISTORICAL_RANK_REPAIR_TARGET_UNBOUND",
                    status=409,
                )
            if resolve_effective_rank_target(
                db,
                source_target=source,
                learning_contract_version_id=contract.id,
            ):
                direct_pairs.add(pair)
                continue
            if (
                source.identity_status != "legacy_provisional"
                or source.status != "active"
                or source.dimension != "recognition"
                or source.target_depth != "standard"
                or not source.objective_statement.strip()
            ):
                raise AppError(
                    "历史能力目标不满足精确迁移条件，修复已停止",
                    code="HISTORICAL_RANK_REPAIR_TARGET_UNSUPPORTED",
                    status=409,
                )
            previous = _latest_decision(
                db,
                contract_id=contract.id,
                target_id=source.id,
            )
            if previous is not None:
                raise AppError(
                    "历史能力目标已有未生效的审计决定，修复已停止",
                    code="HISTORICAL_RANK_REPAIR_DECISION_CONFLICT",
                    status=409,
                )
            candidate = candidates.setdefault(
                pair,
                {
                    "contract": contract,
                    "section": section,
                    "seriesId": series_id,
                    "source": source,
                    "quizIds": set(),
                },
            )
            if candidate["seriesId"] != series_id:
                raise AppError(
                    "同一冻结能力目标跨越了不同学习路线，修复已停止",
                    code="HISTORICAL_RANK_REPAIR_ROUTE_CONFLICT",
                    status=409,
                )
            candidate["quizIds"].add(quiz.id)

    created_pairs: set[tuple[str, str]] = set()
    destination_ids: set[str] = set()
    for pair, candidate in sorted(candidates.items()):
        source = candidate["source"]
        destination = materialize_route_target(
            db,
            series_id=candidate["seriesId"],
            statement=source.objective_statement.strip(),
        )
        if (
            destination.dimension != source.dimension
            or destination.target_depth != source.target_depth
        ):
            raise AppError(
                "新旧能力身份的考核维度不一致，修复已停止",
                code="HISTORICAL_RANK_REPAIR_DIMENSION_MISMATCH",
                status=409,
            )
        basis = {
            "sourceContractVersionId": pair[0],
            "sourceAssessmentTargetId": pair[1],
            "destinationAssessmentTargetId": destination.id,
            "sectionId": candidate["section"].id,
            "seriesId": candidate["seriesId"],
            "publishedQuizSetIds": sorted(candidate["quizIds"]),
            "objectiveStatementHash": hashlib.sha256(
                source.objective_statement.strip().encode()
            ).hexdigest(),
            "dimension": source.dimension,
            "targetDepth": source.target_depth,
            "mappingBasis": "exact_frozen_contract_and_published_quiz_binding",
        }
        decision_hash = _hash(
            HISTORICAL_RANK_IDENTITY_RULE_VERSION,
            pair[0],
            pair[1],
            destination.id,
            _dump(basis),
        )
        db.add(
            AssessmentTargetRankIdentityDecision(
                id=_uid("rank_identity_decision"),
                source_contract_version_id=pair[0],
                source_assessment_target_id=pair[1],
                destination_assessment_target_id=destination.id,
                decision="approved",
                basis_json=_dump(basis),
                rule_version=HISTORICAL_RANK_IDENTITY_RULE_VERSION,
                decision_hash=decision_hash,
                actor_kind="system_maintenance",
                actor_id="historical_rank_repair",
            )
        )
        created_pairs.add(pair)
        destination_ids.add(destination.id)
    db.flush()

    affected_users: set[str] = set()
    if created_pairs:
        source_ids = {target_id for _, target_id in created_pairs}
        for observation in db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.assessment_target_id.in_(source_ids)
            )
        ).all():
            if (
                observation.learning_contract_version_id,
                observation.assessment_target_id,
            ) in created_pairs:
                affected_users.add(observation.user_id)

    replay_totals: defaultdict[str, int] = defaultdict(int)
    for user_id in sorted(affected_users):
        replay = rebuild_assessment_projections(db, user_id=user_id)
        for key in (
            "observations",
            "qualifiedRankObservations",
            "knowledgeNodeStates",
        ):
            replay_totals[key] += int(replay.get(key, 0))
    db.flush()
    skipped_attempts = int(
        db.scalar(
            select(func.count(QuizAttempt.id)).where(
                QuizAttempt.quiz_set_id.in_(skipped_quiz_ids)
            )
        )
        or 0
    ) if skipped_quiz_ids else 0
    return {
        "ruleVersion": HISTORICAL_RANK_IDENTITY_RULE_VERSION,
        "publishedQuizSetsScanned": len(published),
        "eligibleQuizSets": len(eligible_published),
        "legacyQuizSetsSkipped": len(skipped_quiz_ids),
        "legacyAttemptsWithoutTargetEvidence": skipped_attempts,
        "alreadySettleablePairs": len(direct_pairs),
        "identityDecisionsCreated": len(created_pairs),
        "destinationTargets": len(destination_ids),
        "affectedLearners": len(affected_users),
        "replayTotals": dict(replay_totals),
    }
