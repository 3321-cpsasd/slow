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
    AssessmentAnswerVersion,
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    AssessmentDistractorDiagnostic,
    ContentBlockVersion,
    QuizSet,
)


VERSIONED_LEGACY_QUIZ_SCHEMA = "versioned_legacy_quiz_v1"
BLIND_ANSWER_SCHEMA_VERSION = "assessment_answer_v1"
BLIND_ANSWER_RULE_VERSION = "answer_from_option_verdicts_v1"
ALIGNMENT_GATED_ANSWER_AUTHORITY = "alignment_gated_model_v1"
ALIGNMENT_GATED_ANSWER_RULE_VERSION = "answer_after_semantic_alignment_v1"
BLIND_LESSON_SCHEMA_VERSION = "generated_lesson_composition_candidate_v8"
TRUSTED_ASSESSMENT_SCHEMA_VERSIONS = {
    "generated_lesson_composition_candidate_v7",
    BLIND_LESSON_SCHEMA_VERSION,
}
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
        options = payload.get("options", [])
        correct_indexes = payload.get("correct", [])
        if (
            not isinstance(options, list)
            or not 3 <= len(options) <= 6
            or not isinstance(correct_indexes, list)
            or not correct_indexes
            or any(
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(options)
                for index in correct_indexes
            )
        ):
            raise AppError(
                "题目的答案结构无效",
                code="ASSESSMENT_ANSWER_INVALID",
                status=502,
            )
        option_ids = [f"O{index}" for index in range(1, len(options) + 1)]
        payload["optionIds"] = option_ids
        authority_kind = str(
            payload.get("answerAuthority")
            or payload.get("answer_authority")
            or ""
        ).strip()
        persist_answer_version = authority_kind in {
            "blind_model_adjudication_v1",
            "deterministic_rule_v1",
            "reviewed_package_v1",
            "demo_fixture_v1",
            ALIGNMENT_GATED_ANSWER_AUTHORITY,
        }
        answer_rule_version = (
            ALIGNMENT_GATED_ANSWER_RULE_VERSION
            if authority_kind == ALIGNMENT_GATED_ANSWER_AUTHORITY
            else BLIND_ANSWER_RULE_VERSION
        )
        correct_option_ids = [option_ids[index] for index in correct_indexes]
        raw_option_verdicts = (
            payload.get("optionVerdicts")
            or payload.get("option_verdicts")
            or []
        )
        option_verdicts = [
            {
                "optionId": item.get("optionId") or item.get("option_id"),
                "decision": item.get("decision"),
                "evidenceBlockKey": (
                    item.get("evidenceBlockKey")
                    or item.get("evidence_block_key")
                ),
                "rationale": item.get("rationale", ""),
                "causeCode": item.get("causeCode") or item.get("cause_code", ""),
            }
            for item in raw_option_verdicts
        ]
        payload["answerAuthority"] = authority_kind
        payload["optionVerdicts"] = option_verdicts
        payload.pop("answer_authority", None)
        payload.pop("option_verdicts", None)
        explanation = str(payload.get("explanation") or "").strip()
        answer_material = {
            "assessmentItemVersionId": item_id,
            "authorityKind": authority_kind,
            "correctOptionIds": correct_option_ids,
            "optionVerdicts": option_verdicts,
            "explanation": explanation,
            "schemaVersion": BLIND_ANSWER_SCHEMA_VERSION,
            "ruleVersion": answer_rule_version,
        }
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
        item_payload = dict(payload)
        if persist_answer_version:
            for field in (
                "correct", "explanation", "answerAuthority", "answer_authority",
                "optionVerdicts", "option_verdicts",
            ):
                item_payload.pop(field, None)
        db.add(
            AssessmentItemVersion(
                id=item_id,
                quiz_set_id=quiz.id,
                assessment_target_id=target_id,
                position=position,
                item_key=item_key,
                payload_json=_dump(item_payload),
            )
        )
        if persist_answer_version:
            db.flush()
            db.add(AssessmentAnswerVersion(
                id=uid("assessment_answer"),
                assessment_item_version_id=item_id,
                authority_kind=authority_kind,
                correct_option_ids_json=_dump(correct_option_ids),
                option_verdicts_json=_dump(option_verdicts),
                explanation_payload_json=_dump({"text": explanation}),
                schema_version=BLIND_ANSWER_SCHEMA_VERSION,
                rule_version=answer_rule_version,
                verdict_hash=sha256(_dump(answer_material).encode("utf-8")).hexdigest(),
                publication_status="published",
            ))
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
    require_answer_versions: bool = False,
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
    answer_rows = db.scalars(
        select(AssessmentAnswerVersion).where(
            AssessmentAnswerVersion.assessment_item_version_id.in_(
                [row.id for row in rows]
            )
        )
    ).all()
    if not answer_rows and (
        require_answer_versions
        or any("correct" not in question for question in questions)
    ):
        raise AppError(
            "题集缺少独立答案版本，不能作为正式学习证据",
            code="ASSESSMENT_ANSWER_VERSION_MISSING",
            status=409,
        )
    if answer_rows:
        answer_by_item = {row.assessment_item_version_id: row for row in answer_rows}
        if set(answer_by_item) != {row.id for row in rows}:
            raise AppError(
                "题集的答案版本不完整",
                code="ASSESSMENT_ANSWER_VERSION_INCOMPLETE",
                status=409,
            )
        hydrated = []
        for row, question in zip(rows, questions, strict=True):
            answer = answer_by_item[row.id]
            if answer.publication_status != "published":
                raise AppError(
                    "题目答案已经撤回",
                    code="ASSESSMENT_ANSWER_WITHDRAWN",
                    status=409,
                )
            option_ids = question.get("optionIds", [])
            correct_option_ids = _load(answer.correct_option_ids_json, [])
            if (
                len(option_ids) != len(question.get("options", []))
                or len(option_ids) != len(set(option_ids))
                or any(item not in option_ids for item in correct_option_ids)
            ):
                raise AppError(
                    "题目选项与答案版本不一致",
                    code="ASSESSMENT_ANSWER_VERSION_INVALID",
                    status=409,
                )
            explanation_payload = _load(answer.explanation_payload_json, {})
            hydrated.append({
                **question,
                "correct": [option_ids.index(item) for item in correct_option_ids],
                "explanation": str(explanation_payload.get("text") or ""),
                "answerAuthority": answer.authority_kind,
                "optionVerdicts": _load(answer.option_verdicts_json, []),
            })
        questions = hydrated
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
