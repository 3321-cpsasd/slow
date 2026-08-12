"""Deterministic validation and atomic publication for remediation quizzes.

The remediation model output is only a candidate. This module binds it to the
failed attempt's frozen content and Learning Contract before any authoritative
QuizSet or Remediation row is staged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import (
    AssessmentTarget,
    ContentBlockAssessmentTarget,
    ContentBlockVersion,
    ContentVersion,
    GenerationRun,
    GovernanceDecisionSnapshot,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    QuizAttempt,
    QuizSet,
    Remediation,
    Section,
)
from ..modules.learning.assessment_items import publish_assessment_item_versions
from ..modules.learning.content_governance_store import governance_view_for_quiz


REMEDIATION_SCHEMA_VERSION = "remediation_candidate_v1"
REMEDIATION_RULE_VERSION = "remediation_contract_gate_v1"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _reject(code: str, message: str, **details: Any) -> None:
    raise AppError(
        message,
        code=code,
        status=502,
        retryable=True,
        details=details,
    )


@dataclass(frozen=True)
class PublishedRemediation:
    quiz: QuizSet
    remediation: Remediation
    governance: dict


def publish_remediation_candidate(
    db: Session,
    *,
    uid: Callable[[str], str],
    section: Section,
    contract: LearningContractVersion,
    source_content: ContentVersion,
    source_quiz: QuizSet,
    source_attempt: QuizAttempt,
    generation_run: GenerationRun,
    quiz_generation: int,
    questions: Sequence[dict],
    prior_questions: Sequence[dict],
    remediation_blocks: Sequence[dict],
    failed_target_ids: set[str],
    strategy: str,
    diagnosis_snapshot: Sequence[dict] = (),
    superseded_remediation: Remediation | None = None,
) -> PublishedRemediation:
    """Validate a remediation candidate and stage its authoritative rows."""

    if (
        source_attempt.quiz_set_id != source_quiz.id
        or source_quiz.content_version_id != source_content.id
        or source_quiz.learning_contract_version_id != contract.id
        or source_content.learning_contract_version_id != contract.id
        or source_content.section_id != section.id
        or source_quiz.section_id != section.id
    ):
        _reject(
            "REMEDIATION_SOURCE_VERSION_MISMATCH",
            "补救候选没有绑定原答题使用的正文、题集与学习契约",
            attemptId=source_attempt.id,
            quizSetId=source_quiz.id,
            contentVersionId=source_content.id,
            contractVersionId=contract.id,
        )
    if source_content.publication_status != "published":
        _reject(
            "REMEDIATION_SOURCE_NOT_PUBLISHED",
            "补救候选引用的原正文不是正式发布版本",
            contentVersionId=source_content.id,
        )
    source_governance = governance_view_for_quiz(db, source_quiz.id)
    if not source_governance or not (
        source_governance["allowed"]
        and source_governance["assessmentEligible"]
    ):
        _reject(
            "REMEDIATION_SOURCE_GOVERNANCE_REQUIRED",
            "原答题题集已经不具备正式证据资格，不能生成补救题",
            quizSetId=source_quiz.id,
        )
    if not failed_target_ids:
        _reject(
            "REMEDIATION_TARGETS_MISSING",
            "补救候选缺少原答题中的失败目标",
            attemptId=source_attempt.id,
        )

    contract_rows = db.execute(
        select(LearningContractAssessmentTarget, AssessmentTarget)
        .join(
            AssessmentTarget,
            AssessmentTarget.id
            == LearningContractAssessmentTarget.assessment_target_id,
        )
        .where(
            LearningContractAssessmentTarget.contract_version_id == contract.id
        )
        .order_by(LearningContractAssessmentTarget.position)
    ).all()
    target_by_id = {target.id: (binding, target) for binding, target in contract_rows}
    target_by_objective = {
        target.objective_statement.strip(): target.id
        for _binding, target in contract_rows
    }
    if not failed_target_ids.issubset(target_by_id):
        _reject(
            "REMEDIATION_TARGET_UNBOUND",
            "失败目标不属于原答题冻结的 Learning Contract",
            assessmentTargetIds=sorted(failed_target_ids),
            contractVersionId=contract.id,
        )

    block_rows = db.scalars(
        select(ContentBlockVersion)
        .where(ContentBlockVersion.content_version_id == source_content.id)
        .order_by(ContentBlockVersion.position)
    ).all()
    if not block_rows:
        _reject(
            "REMEDIATION_SOURCE_BLOCKS_MISSING",
            "原正文缺少不可变正文块版本",
            contentVersionId=source_content.id,
        )
    target_rows = db.scalars(
        select(ContentBlockAssessmentTarget).where(
            ContentBlockAssessmentTarget.content_block_version_id.in_(
                [row.id for row in block_rows]
            )
        )
    ).all()
    taught_by_block: dict[str, set[str]] = {}
    for row in target_rows:
        taught_by_block.setdefault(row.content_block_version_id, set()).add(
            row.assessment_target_id
        )
    block_payloads = {
        str(item.get("id") or ""): item
        for item in json.loads(source_content.blocks_json or "[]")
        if isinstance(item, dict)
    }
    for row in block_rows:
        payload = block_payloads.get(row.id, {})
        declared = {
            str(target_id)
            for target_id in payload.get("assessmentTargetIds", [])
            if str(target_id) in target_by_id
        }
        declared.update(
            target_by_objective[objective.strip()]
            for objective in payload.get("assessment_objectives", [])
            if isinstance(objective, str)
            and objective.strip() in target_by_objective
        )
        taught_by_block.setdefault(row.id, set()).update(declared)
    block_by_id = {row.id: row for row in block_rows}

    taught_in_remediation: set[str] = set()
    for block in remediation_blocks:
        objectives = block.get("assessment_objectives", [])
        if not isinstance(objectives, list):
            _reject(
                "REMEDIATION_BLOCK_TARGET_INVALID",
                "补救教学块的目标绑定结构无效",
            )
        for objective in objectives:
            target_id = target_by_objective.get(str(objective).strip())
            if not target_id or target_id not in failed_target_ids:
                _reject(
                    "REMEDIATION_BLOCK_TARGET_UNBOUND",
                    "补救教学块引用了失败目标之外的学习目标",
                    objective=str(objective),
                )
            taught_in_remediation.add(target_id)
    missing_teaching = failed_target_ids - taught_in_remediation
    if missing_teaching:
        _reject(
            "REMEDIATION_TARGET_NOT_TAUGHT",
            "补救教学没有覆盖全部失败目标",
            assessmentTargetIds=sorted(missing_teaching),
        )

    normalized_questions: list[dict] = []
    evidence_block_ids_by_position: list[list[str]] = []
    assessed_target_ids: set[str] = set()
    for position, question in enumerate(questions):
        target_id = str(question.get("assessmentTargetId") or "").strip()
        if target_id not in failed_target_ids or target_id not in target_by_id:
            _reject(
                "REMEDIATION_QUESTION_TARGET_UNBOUND",
                "补救题引用了失败目标之外的学习目标",
                position=position,
                assessmentTargetId=target_id,
            )
        prior = (
            prior_questions[position]
            if position < len(prior_questions)
            else None
        )
        if not prior:
            _reject(
                "REMEDIATION_SOURCE_ITEM_REQUIRED",
                "补救题缺少被替换的不可变原题",
                position=position,
            )
        prior_target_id = str(
            prior.get("assessmentTargetId") or ""
        ).strip()
        if prior_target_id != target_id:
            _reject(
                "REMEDIATION_SOURCE_ITEM_TARGET_MISMATCH",
                "补救题与被替换原题没有绑定同一个失败目标",
                position=position,
                assessmentTargetId=target_id,
                sourceAssessmentTargetId=prior_target_id,
            )
        source_item_id = str(prior.get("id") or "").strip()
        evidence_ids = prior.get("evidenceBlockIds", [])
        if (
            not source_item_id
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or any(not isinstance(item, str) for item in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            _reject(
                "REMEDIATION_EVIDENCE_BLOCK_REQUIRED",
                "被替换原题缺少不可变题目身份或正文证据绑定",
                position=position,
                assessmentTargetId=target_id,
            )
        for evidence_id in evidence_ids:
            block = block_by_id.get(evidence_id)
            if not block:
                _reject(
                    "REMEDIATION_EVIDENCE_BLOCK_UNBOUND",
                    "被替换原题引用了不存在的冻结正文块",
                    position=position,
                    contentBlockVersionId=evidence_id,
                )
            if target_id not in taught_by_block.get(block.id, set()):
                _reject(
                    "REMEDIATION_EVIDENCE_TARGET_MISMATCH",
                    "补救题引用的原正文块没有教授同一个失败目标",
                    position=position,
                    contentBlockVersionId=block.id,
                    assessmentTargetId=target_id,
                )
        binding, target = target_by_id[target_id]
        payload = dict(question)
        payload.pop("id", None)
        payload.pop("claim_block_indexes", None)
        payload["itemKey"] = f"remediation_{quiz_generation}_{position + 1}"
        payload["assessmentTargetId"] = target_id
        payload["objective"] = target.objective_statement
        payload["core"] = binding.required
        payload["evidenceBlockIds"] = evidence_ids
        payload["generationRunId"] = generation_run.id
        payload["sourceQuizSetId"] = source_quiz.id
        payload["sourceAssessmentItemVersionId"] = source_item_id
        payload["publicationRuleVersion"] = REMEDIATION_RULE_VERSION
        payload["equivalenceGroupId"] = (
            f"{target_id}:remediation:{source_attempt.id}:slot:{position}"
        )
        normalized_questions.append(payload)
        evidence_block_ids_by_position.append(list(evidence_ids))
        assessed_target_ids.add(target_id)
    missing_assessment = failed_target_ids - assessed_target_ids
    if missing_assessment:
        _reject(
            "REMEDIATION_TARGET_NOT_ASSESSED",
            "补救题没有覆盖全部失败目标",
            assessmentTargetIds=sorted(missing_assessment),
        )

    quiz = QuizSet(
        id=uid("quiz"),
        section_id=section.id,
        content_version_id=source_content.id,
        learning_contract_version_id=contract.id,
        generation=quiz_generation,
        questions_json="[]",
        publication_status="published",
        schema_version=REMEDIATION_SCHEMA_VERSION,
    )
    db.add(quiz)
    db.flush()
    publish_assessment_item_versions(
        db,
        quiz=quiz,
        questions=normalized_questions,
        evidence_block_ids_by_position=evidence_block_ids_by_position,
        uid=uid,
    )

    remediation_payloads: list[dict] = []
    for position, block in enumerate(remediation_blocks, 1):
        payload = dict(block)
        payload["id"] = f"block_remediation_{quiz.id}_{position}"
        payload["version"] = quiz.generation
        remediation_payloads.append(payload)
    remediation = Remediation(
        id=uid("remediation"),
        section_id=section.id,
        attempt_id=source_attempt.id,
        replacement_quiz_id=quiz.id,
        supersedes_id=(
            superseded_remediation.id if superseded_remediation else None
        ),
        blocks_json=_dump(remediation_payloads),
        objectives_json=_dump(
            sorted(target_by_id[target_id][1].objective_statement for target_id in failed_target_ids)
        ),
        strategy=strategy,
        diagnosis_snapshot_json=_dump(list(diagnosis_snapshot)),
    )
    db.add(remediation)

    gate_input = {
        "contractVersionId": contract.id,
        "sourceContentVersionId": source_content.id,
        "sourceQuizSetId": source_quiz.id,
        "sourceAttemptId": source_attempt.id,
        "failedAssessmentTargetIds": sorted(failed_target_ids),
        "questions": [
            {
                "itemKey": question["itemKey"],
                "assessmentTargetId": question["assessmentTargetId"],
                "evidenceBlockIds": question["evidenceBlockIds"],
            }
            for question in normalized_questions
        ],
    }
    snapshot = GovernanceDecisionSnapshot(
        id=uid("governance_decision"),
        decision_scope="quiz_publication",
        content_version_id=source_content.id,
        quiz_set_id=quiz.id,
        learning_contract_version_id=contract.id,
        requested_mode="deterministic",
        mode="contract_boundary",
        allowed=True,
        assessment_eligible=True,
        reasons_json="[]",
        rule_version=REMEDIATION_RULE_VERSION,
        input_hash=_hash(gate_input),
        actor_kind="generation_attempt",
        actor_id=generation_run.id,
        idempotency_key=f"remediation-v1:quiz:{quiz.id}",
    )
    db.add(snapshot)
    db.flush()
    return PublishedRemediation(
        quiz=quiz,
        remediation=remediation,
        governance={
            "decisionId": snapshot.id,
            "scope": snapshot.decision_scope,
            "requestedMode": snapshot.requested_mode,
            "mode": snapshot.mode,
            "allowed": snapshot.allowed,
            "assessmentEligible": snapshot.assessment_eligible,
            "reasons": [],
            "ruleVersion": snapshot.rule_version,
        },
    )
