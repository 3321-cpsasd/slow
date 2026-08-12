"""Deterministic, abstaining diagnosis from immutable quiz evidence.

The diagnostic labels are hypotheses used to choose a teaching move. They are
never promoted to mastery evidence and never inferred when the selected answer
does not carry enough concordant signals.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentDistractorDiagnostic,
    AssessmentItemVersion,
    QuizAttempt,
    RemediationDiagnosis,
)
from .assessment_items import immutable_questions_for_quiz
from ...infrastructure.tables import QuizSet


DIAGNOSIS_RULE_VERSION = "remediation_diagnosis_v1"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STRATEGY_BY_CAUSE = {
    "prerequisite_gap": "prerequisite_supplement",
    "concept_confusion": "contrastive_definition",
    "mechanism_reasoning_break": "mechanism_walkthrough",
    "boundary_comparison_error": "boundary_matrix",
    "application_transfer_failure": "guided_transfer",
    INSUFFICIENT_EVIDENCE: "diagnostic_probe",
}


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def diagnose_failed_attempt(db: Session, attempt: QuizAttempt) -> list[dict]:
    """Persist one diagnosis per failed target, abstaining on weak/conflicting data."""

    quiz = db.get(QuizSet, attempt.quiz_set_id)
    if not quiz:
        raise AppError("原答题题集不存在", code="REMEDIATION_QUIZ_NOT_FOUND", status=409)
    questions = immutable_questions_for_quiz(
        db, quiz, require_versions=True, require_evidence=True
    )
    results = _load(attempt.results_json, [])
    if len(questions) != len(results):
        raise AppError(
            "原答题结果与冻结题目不一致",
            code="REMEDIATION_DIAGNOSIS_INPUT_INVALID",
            status=409,
        )

    item_ids = [str(question.get("id") or "") for question in questions]
    diagnostic_rows = list(db.scalars(
        select(AssessmentDistractorDiagnostic).where(
            AssessmentDistractorDiagnostic.assessment_item_version_id.in_(item_ids)
        )
    )) if item_ids else []
    diagnostic_by_option = {
        (row.assessment_item_version_id, row.option_index): row
        for row in diagnostic_rows
    }
    evidence_by_target: dict[str, list[dict]] = {}
    failed_targets: set[str] = set()
    for question, result in zip(questions, results, strict=True):
        if result.get("correct") is True:
            continue
        target_id = str(question.get("assessmentTargetId") or "")
        item_id = str(question.get("id") or "")
        if not target_id or not item_id:
            continue
        failed_targets.add(target_id)
        incorrect = result.get("incorrectOptions")
        if not isinstance(incorrect, list):
            selected = result.get("selectedOptions", [])
            correct = set(result.get("correctOptions", []))
            incorrect = [index for index in selected if index not in correct]
        for option_index in incorrect:
            row = diagnostic_by_option.get((item_id, option_index))
            if row:
                evidence_by_target.setdefault(target_id, []).append({
                    "assessmentItemVersionId": item_id,
                    "optionIndex": option_index,
                    "causeCode": row.cause_code,
                })

    snapshots: list[dict] = []
    for target_id in sorted(failed_targets):
        evidence = evidence_by_target.get(target_id, [])
        counts = Counter(item["causeCode"] for item in evidence)
        if len(counts) == 1 and next(iter(counts.values())) >= 2:
            cause_code = next(iter(counts))
            status, confidence = "supported", 0.9
        elif len(counts) == 1 and next(iter(counts.values())) == 1:
            cause_code = next(iter(counts))
            status, confidence = "tentative", 0.6
        else:
            cause_code = INSUFFICIENT_EVIDENCE
            status, confidence = "abstained", 0.0
        diagnosis_input = {
            "attemptId": attempt.id,
            "assessmentTargetId": target_id,
            "evidence": evidence,
            "ruleVersion": DIAGNOSIS_RULE_VERSION,
        }
        input_hash = sha256(_dump(diagnosis_input).encode("utf-8")).hexdigest()
        row = db.scalar(select(RemediationDiagnosis).where(
            RemediationDiagnosis.attempt_id == attempt.id,
            RemediationDiagnosis.assessment_target_id == target_id,
            RemediationDiagnosis.rule_version == DIAGNOSIS_RULE_VERSION,
        ))
        if row:
            if row.input_hash != input_hash:
                raise AppError(
                    "补救诊断的冻结证据已经改变",
                    code="REMEDIATION_DIAGNOSIS_INPUT_CHANGED",
                    status=409,
                )
        else:
            row = RemediationDiagnosis(
                id=f"remediation_diagnosis_{uuid4().hex}",
                attempt_id=attempt.id,
                assessment_target_id=target_id,
                cause_code=cause_code,
                status=status,
                confidence=confidence,
                evidence_json=_dump(evidence),
                input_hash=input_hash,
                rule_version=DIAGNOSIS_RULE_VERSION,
            )
            db.add(row)
            db.flush()
        snapshots.append({
            "assessmentTargetId": target_id,
            "causeCode": row.cause_code,
            "status": row.status,
            "confidence": row.confidence,
            "evidenceCount": len(_load(row.evidence_json, [])),
            "recommendedStrategy": STRATEGY_BY_CAUSE[row.cause_code],
        })
    return snapshots


def choose_remediation_strategy(diagnoses: list[dict]) -> str:
    """Choose one bounded generation strategy without pretending certainty."""

    actionable = [
        item for item in diagnoses if item.get("status") in {"supported", "tentative"}
    ]
    if not actionable:
        return STRATEGY_BY_CAUSE[INSUFFICIENT_EVIDENCE]
    actionable.sort(
        key=lambda item: (
            item.get("status") != "supported",
            -float(item.get("confidence") or 0),
            str(item.get("assessmentTargetId") or ""),
        )
    )
    return str(actionable[0]["recommendedStrategy"])
