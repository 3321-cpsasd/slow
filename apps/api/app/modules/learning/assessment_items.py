"""Immutable assessment-item publication and reads.

``QuizSet.questions_json`` remains a compatibility projection for older data.
Newly published quizzes use ``AssessmentItemVersion`` as the scoring authority.
"""

from __future__ import annotations

import json
from hashlib import sha256
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    AssessmentDistractorDiagnostic,
    ContentBlockVersion,
    QuizSet,
)


VERSIONED_LEGACY_QUIZ_SCHEMA = "versioned_legacy_quiz_v1"
DIAGNOSTIC_CAUSES = {
    "prerequisite_gap",
    "concept_confusion",
    "mechanism_reasoning_break",
    "boundary_comparison_error",
    "application_transfer_failure",
}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def publish_assessment_item_versions(
    db: Session,
    *,
    quiz: QuizSet,
    questions: Sequence[dict],
    evidence_block_ids_by_position: Sequence[Sequence[str]],
    uid: Callable[[str], str],
) -> list[dict]:
    """Persist one immutable item row and its explicit evidence bindings per question."""

    if len(questions) != len(evidence_block_ids_by_position):
        raise AppError(
            "题目与正文证据绑定数量不一致",
            code="ASSESSMENT_ITEM_EVIDENCE_COUNT_MISMATCH",
            status=502,
        )
    existing = db.scalars(
        select(AssessmentItemVersion).where(
            AssessmentItemVersion.quiz_set_id == quiz.id
        )
    ).all()
    if existing:
        raise AppError(
            "题集已经发布过不可变题目版本",
            code="ASSESSMENT_ITEM_VERSION_ALREADY_PUBLISHED",
            status=409,
        )

    evidence_ids = {
        block_id
        for block_ids in evidence_block_ids_by_position
        for block_id in block_ids
    }
    valid_evidence_ids = set()
    if evidence_ids:
        valid_evidence_ids = set(
            db.scalars(
                select(ContentBlockVersion.id).where(
                    ContentBlockVersion.id.in_(evidence_ids),
                    ContentBlockVersion.content_version_id
                    == quiz.content_version_id,
                )
            ).all()
        )
    if valid_evidence_ids != evidence_ids:
        raise AppError(
            "题目引用了不属于当前正文版本的证据块",
            code="ASSESSMENT_EVIDENCE_BLOCK_UNBOUND",
            status=502,
        )

    item_keys: set[str] = set()
    payloads: list[dict] = []
    pending_bindings: list[tuple[str, str]] = []
    pending_diagnostics: list[tuple[str, int, str, str, str]] = []
    for position, (question, evidence_block_ids) in enumerate(
        zip(questions, evidence_block_ids_by_position, strict=True)
    ):
        target_id = str(question.get("assessmentTargetId") or "").strip()
        if not target_id:
            raise AppError(
                "题目缺少稳定学习目标",
                code="ASSESSMENT_TARGET_UNBOUND",
                status=502,
            )
        item_key = str(question.get("itemKey") or f"q{position + 1}").strip()
        if not item_key or len(item_key) > 64 or item_key in item_keys:
            raise AppError(
                "题目局部标识无效或重复",
                code="ASSESSMENT_ITEM_KEY_INVALID",
                status=502,
            )
        item_keys.add(item_key)
        item_id = str(question.get("id") or uid("assessment_item"))
        normalized_evidence_ids = list(dict.fromkeys(evidence_block_ids))
        payload = dict(question)
        payload["id"] = item_id
        payload["itemKey"] = item_key
        payload["assessmentTargetId"] = target_id
        payload["evidenceBlockIds"] = normalized_evidence_ids
        diagnostics = payload.get("distractorDiagnostics")
        if diagnostics is None:
            diagnostics = payload.get("distractor_diagnostics", [])
            payload["distractorDiagnostics"] = [
                {
                    "optionIndex": item.get("option_index"),
                    "causeCode": item.get("cause_code"),
                    "rationale": item.get("rationale", ""),
                }
                for item in diagnostics
            ]
            payload.pop("distractor_diagnostics", None)
            diagnostics = payload["distractorDiagnostics"]
        options = payload.get("options", [])
        correct = set(payload.get("correct", []))
        seen_diagnostic_indexes: set[int] = set()
        for diagnostic in diagnostics:
            option_index = diagnostic.get("optionIndex")
            cause_code = str(diagnostic.get("causeCode") or "")
            rationale = str(diagnostic.get("rationale") or "")
            if (
                not isinstance(option_index, int)
                or isinstance(option_index, bool)
                or option_index < 0
                or option_index >= len(options)
                or option_index in correct
                or option_index in seen_diagnostic_indexes
                or cause_code not in DIAGNOSTIC_CAUSES
            ):
                raise AppError(
                    "题目的错误选项诊断绑定无效",
                    code="ASSESSMENT_DISTRACTOR_DIAGNOSTIC_INVALID",
                    status=502,
                )
            seen_diagnostic_indexes.add(option_index)
            pending_diagnostics.append((
                item_id,
                option_index,
                sha256(str(options[option_index]).encode("utf-8")).hexdigest(),
                cause_code,
                rationale,
            ))
        if diagnostics and seen_diagnostic_indexes != set(range(len(options))) - correct:
            raise AppError(
                "题目的错误选项诊断覆盖不完整",
                code="ASSESSMENT_DISTRACTOR_DIAGNOSTIC_INCOMPLETE",
                status=502,
            )
        payloads.append(payload)
        db.add(
            AssessmentItemVersion(
                id=item_id,
                quiz_set_id=quiz.id,
                assessment_target_id=target_id,
                position=position,
                item_key=item_key,
                payload_json=_dump(payload),
            )
        )
        pending_bindings.extend(
            (item_id, block_id) for block_id in normalized_evidence_ids
        )
    db.flush()
    for item_id, block_id in pending_bindings:
        db.add(
            AssessmentItemEvidenceBlock(
                id=uid("item_evidence_block"),
                assessment_item_version_id=item_id,
                content_block_version_id=block_id,
            )
        )
    for item_id, option_index, option_hash, cause_code, rationale in pending_diagnostics:
        db.add(AssessmentDistractorDiagnostic(
            id=uid("distractor_diagnostic"),
            assessment_item_version_id=item_id,
            option_index=option_index,
            option_hash=option_hash,
            cause_code=cause_code,
            rationale=rationale,
        ))
    quiz.questions_json = _dump(payloads)
    return payloads


def immutable_questions_for_quiz(
    db: Session,
    quiz: QuizSet,
    *,
    require_versions: bool = False,
    require_evidence: bool = False,
) -> list[dict]:
    """Load the immutable scoring payload, with an explicit legacy fallback."""

    rows = db.scalars(
        select(AssessmentItemVersion)
        .where(AssessmentItemVersion.quiz_set_id == quiz.id)
        .order_by(AssessmentItemVersion.position)
    ).all()
    if not rows:
        if require_versions or quiz.schema_version != "legacy":
            raise AppError(
                "题集缺少不可变题目版本，不能作为正式学习证据",
                code="ASSESSMENT_ITEM_VERSION_MISSING",
                status=409,
            )
        return _load(quiz.questions_json, [])

    if [row.position for row in rows] != list(range(len(rows))):
        raise AppError(
            "题目版本序列不完整",
            code="ASSESSMENT_ITEM_VERSION_INCOMPLETE",
            status=409,
        )
    questions = [_load(row.payload_json, {}) for row in rows]
    if any(
        not question
        or question.get("id") != row.id
        or question.get("assessmentTargetId") != row.assessment_target_id
        for row, question in zip(rows, questions, strict=True)
    ):
        raise AppError(
            "题目版本载荷与稳定身份不一致",
            code="ASSESSMENT_ITEM_VERSION_INVALID",
            status=409,
        )
    bindings = db.scalars(
        select(AssessmentItemEvidenceBlock).where(
            AssessmentItemEvidenceBlock.assessment_item_version_id.in_(
                [row.id for row in rows]
            )
        )
    ).all()
    bound_ids_by_item: dict[str, set[str]] = {}
    for binding in bindings:
        bound_ids_by_item.setdefault(
            binding.assessment_item_version_id,
            set(),
        ).add(binding.content_block_version_id)
    for row, question in zip(rows, questions, strict=True):
        expected_ids = set(question.get("evidenceBlockIds", []))
        bound_ids = bound_ids_by_item.get(row.id, set())
        if expected_ids != bound_ids or (require_evidence and not bound_ids):
            raise AppError(
                "题目版本缺少完整的正文证据绑定",
                code="ASSESSMENT_ITEM_EVIDENCE_INCOMPLETE",
                status=409,
            )
    return questions
