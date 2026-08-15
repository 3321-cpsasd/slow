"""Deterministic publication for quizzes derived from frozen assessment items.

Review and reinforcement models only produce candidates. This module resolves
the original immutable item, inherits its exact taught-block evidence, and
stages the new quiz, item, answer, evidence, diagnostics, and governance rows as
one caller-owned transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    AssessmentTarget,
    ContentBlockAssessmentTarget,
    ContentBlockVersion,
    ContentVersion,
    GovernanceDecisionSnapshot,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    LearningEvidenceInvalidation,
    QuizSet,
    Section,
)
from .assessment_items import (
    ALIGNMENT_GATED_ANSWER_AUTHORITY,
    immutable_questions_for_quiz,
    publish_assessment_item_versions,
)
from .content_governance_store import governance_view_for_quiz


DERIVED_ASSESSMENT_SCHEMA_VERSION = "derived_assessment_v1"
DERIVED_ASSESSMENT_RULE_VERSION = "derived_assessment_contract_gate_v1"
PUBLISHABLE_DERIVED_ANSWER_AUTHORITIES = {
    "blind_model_adjudication_v1",
    "deterministic_rule_v1",
    "reviewed_package_v1",
    "demo_fixture_v1",
    ALIGNMENT_GATED_ANSWER_AUTHORITY,
}
DerivedQuizKind = Literal["review", "reinforcement"]

_PRIVATE_QUESTION_FIELDS = {
    "correct",
    "explanation",
    "claim_block_indexes",
    "distractor_diagnostics",
    "distractorDiagnostics",
    "id",
    "itemKey",
    "optionIds",
    "answerAuthority",
    "answer_authority",
    "optionVerdicts",
    "option_verdicts",
    "evidenceBlockIds",
    "sourceQuizSetId",
    "sourceAssessmentItemVersionId",
    "publicationRuleVersion",
    "equivalenceGroupId",
    "assessmentTargetId",
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def question_signature(question: dict) -> str:
    return _hash({
        "prompt": question.get("prompt", ""),
        "options": question.get("options", []),
        "correct": sorted(question.get("correct", [])),
    })


def questions_are_substantively_different(previous: dict, candidate: dict) -> bool:
    previous_prompt = _normalized(str(previous.get("prompt", "")))
    candidate_prompt = _normalized(str(candidate.get("prompt", "")))
    previous_options = frozenset(
        _normalized(str(item)) for item in previous.get("options", [])
    )
    candidate_options = frozenset(
        _normalized(str(item)) for item in candidate.get("options", [])
    )
    return bool(
        candidate_prompt
        and candidate_options
        and candidate_prompt != previous_prompt
        and candidate_options != previous_options
        and question_signature(candidate) != question_signature(previous)
    )


def public_question_view(question: dict) -> dict:
    """Return only the learner-facing fields of an immutable question."""

    return {
        **{
            key: value
            for key, value in question.items()
            if key not in _PRIVATE_QUESTION_FIELDS
        },
        "selectionMode": (
            "multiple"
            if len(set(question.get("correct", []))) > 1
            else "single"
        ),
    }


def with_alignment_gated_answer(question: dict) -> dict:
    """Promote an author-declared answer only after semantic alignment passed."""

    normalized = dict(question)
    authority = str(
        normalized.get("answerAuthority")
        or normalized.get("answer_authority")
        or ""
    ).strip()
    if authority == "legacy_author_declared":
        normalized["answerAuthority"] = ALIGNMENT_GATED_ANSWER_AUTHORITY
    return normalized


@dataclass(frozen=True)
class DerivedQuizSource:
    section: Section
    contract: LearningContractVersion
    content: ContentVersion
    quiz: QuizSet
    item: AssessmentItemVersion
    target: AssessmentTarget
    contract_target: LearningContractAssessmentTarget
    question: dict
    evidence_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class PublishedDerivedQuiz:
    quiz: QuizSet
    question: dict
    governance: dict


def _source_error(code: str, message: str, **details: Any) -> AppError:
    return AppError(message, code=code, status=409, details=details)


def _candidate_error(code: str, message: str, **details: Any) -> AppError:
    return AppError(
        message,
        code=code,
        status=502,
        retryable=True,
        details=details,
    )


def load_derived_quiz_source(
    db: Session,
    *,
    quiz: QuizSet,
    assessment_target_id: str,
    question_position: int,
    expected_section_id: str | None = None,
    expected_content_version_id: str | None = None,
    expected_contract_version_id: str | None = None,
) -> DerivedQuizSource:
    """Resolve and validate one exact immutable source item for derivation."""

    if quiz.publication_status not in {"published", "superseded"}:
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_WITHDRAWN",
            "原题集已经失效，不能继续生成派生测验",
            quizSetId=quiz.id,
        )
    content = db.get(ContentVersion, quiz.content_version_id)
    contract = (
        db.get(LearningContractVersion, quiz.learning_contract_version_id)
        if quiz.learning_contract_version_id
        else None
    )
    section = db.get(Section, quiz.section_id)
    if not content or not contract or not section:
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_MISSING",
            "原题缺少冻结的正文、学习契约或小节",
            quizSetId=quiz.id,
        )
    if (
        content.publication_status not in {"published", "superseded"}
        or content.section_id != section.id
        or content.learning_contract_version_id != contract.id
        or contract.section_id != section.id
        or quiz.content_version_id != content.id
        or quiz.learning_contract_version_id != contract.id
        or (expected_section_id and section.id != expected_section_id)
        or (
            expected_content_version_id
            and content.id != expected_content_version_id
        )
        or (
            expected_contract_version_id
            and contract.id != expected_contract_version_id
        )
    ):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_VERSION_MISMATCH",
            "原题、正文和学习契约版本不一致",
            quizSetId=quiz.id,
            contentVersionId=content.id,
            contractVersionId=contract.id,
        )
    if db.scalar(
        select(LearningEvidenceInvalidation.id).where(
            LearningEvidenceInvalidation.quiz_set_id == quiz.id
        )
    ):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_EVIDENCE_INVALIDATED",
            "原题的学习证据资格已经失效",
            quizSetId=quiz.id,
        )
    governance = governance_view_for_quiz(db, quiz.id)
    if not governance or not (
        governance["allowed"] and governance["assessmentEligible"]
    ):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_GOVERNANCE_REQUIRED",
            "原题集已经不具备正式考核资格",
            quizSetId=quiz.id,
        )

    questions = immutable_questions_for_quiz(
        db,
        quiz,
        require_versions=True,
        require_evidence=True,
        # Historical source items remain valid derivation inputs when their
        # immutable payload, evidence, contract, and governance are intact.
        # They are not scored here. Every newly published derived item still
        # requires and persists a separate immutable answer version below.
        require_answer_versions=False,
    )
    if (
        isinstance(question_position, bool)
        or question_position < 0
        or question_position >= len(questions)
    ):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_ITEM_MISSING",
            "原始观察无法定位到不可变题目版本",
            quizSetId=quiz.id,
            questionPosition=question_position,
        )
    question = questions[question_position]
    if question.get("assessmentTargetId") != assessment_target_id:
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_TARGET_MISMATCH",
            "原始观察与不可变题目的测量目标不一致",
            quizSetId=quiz.id,
            questionPosition=question_position,
            assessmentTargetId=assessment_target_id,
        )
    item = db.get(AssessmentItemVersion, str(question.get("id") or ""))
    target = db.get(AssessmentTarget, assessment_target_id)
    contract_target = db.scalar(
        select(LearningContractAssessmentTarget).where(
            LearningContractAssessmentTarget.contract_version_id == contract.id,
            LearningContractAssessmentTarget.assessment_target_id
            == assessment_target_id,
        )
    )
    if (
        not item
        or item.quiz_set_id != quiz.id
        or item.position != question_position
        or item.assessment_target_id != assessment_target_id
        or not target
        or not contract_target
        or contract_target.diagnostic_only
    ):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_ITEM_INVALID",
            "原题的不可变身份或契约目标绑定不完整",
            quizSetId=quiz.id,
            questionPosition=question_position,
        )

    evidence_ids = tuple(question.get("evidenceBlockIds") or ())
    binding_ids = set(
        db.scalars(
            select(AssessmentItemEvidenceBlock.content_block_version_id).where(
                AssessmentItemEvidenceBlock.assessment_item_version_id == item.id
            )
        )
    )
    if not evidence_ids or set(evidence_ids) != binding_ids:
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_EVIDENCE_INCOMPLETE",
            "原题缺少完整的不可变正文证据绑定",
            assessmentItemVersionId=item.id,
        )
    blocks = {
        row.id: row
        for row in db.scalars(
            select(ContentBlockVersion).where(
                ContentBlockVersion.id.in_(evidence_ids)
            )
        )
    }
    taught_ids = set(
        db.scalars(
            select(ContentBlockAssessmentTarget.content_block_version_id).where(
                ContentBlockAssessmentTarget.content_block_version_id.in_(evidence_ids),
                ContentBlockAssessmentTarget.assessment_target_id
                == assessment_target_id,
                ContentBlockAssessmentTarget.binding_role == "teaches",
            )
        )
    )
    if set(blocks) != set(evidence_ids) or any(
        block.content_version_id != content.id for block in blocks.values()
    ):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_EVIDENCE_CROSS_CONTENT",
            "原题证据块不属于同一份可考核冻结正文",
            assessmentItemVersionId=item.id,
        )
    if taught_ids != set(evidence_ids):
        raise _source_error(
            "DERIVED_QUIZ_SOURCE_EVIDENCE_TARGET_MISMATCH",
            "原题证据块没有教授同一个测量目标",
            assessmentItemVersionId=item.id,
            assessmentTargetId=assessment_target_id,
        )
    return DerivedQuizSource(
        section=section,
        contract=contract,
        content=content,
        quiz=quiz,
        item=item,
        target=target,
        contract_target=contract_target,
        question=question,
        evidence_block_ids=evidence_ids,
    )


def _validate_answer_authority(question: dict) -> None:
    authority = str(
        question.get("answerAuthority")
        or question.get("answer_authority")
        or ""
    ).strip()
    if authority not in PUBLISHABLE_DERIVED_ANSWER_AUTHORITIES:
        raise _candidate_error(
            "DERIVED_QUIZ_ANSWER_AUTHORITY_REQUIRED",
            "派生测验缺少可审计的不可变答案权威",
            answerAuthority=authority,
        )
    if authority != "blind_model_adjudication_v1":
        return
    options = question.get("options", [])
    correct = question.get("correct", [])
    verdicts = question.get("optionVerdicts") or question.get("option_verdicts") or []
    expected_ids = {f"O{index}" for index in range(1, len(options) + 1)}
    verdict_by_id = {
        str(item.get("optionId") or item.get("option_id") or ""): item
        for item in verdicts
        if isinstance(item, dict)
    }
    satisfying_ids = {
        option_id
        for option_id, verdict in verdict_by_id.items()
        if verdict.get("decision") == "satisfies"
    }
    expected_correct_ids = {f"O{index + 1}" for index in correct}
    if (
        set(verdict_by_id) != expected_ids
        or any(
            item.get("decision") not in {"satisfies", "does_not_satisfy"}
            for item in verdict_by_id.values()
        )
        or satisfying_ids != expected_correct_ids
    ):
        raise _candidate_error(
            "DERIVED_QUIZ_ANSWER_AUTHORITY_INVALID",
            "派生测验的盲判裁决与答案不一致",
        )


def publish_derived_quiz_candidate(
    db: Session,
    *,
    uid: Callable[[str], str],
    source: DerivedQuizSource,
    candidate_question: dict,
    kind: DerivedQuizKind,
    quiz_generation: int,
    actor_kind: str,
    actor_id: str,
    equivalence_group_id: str,
) -> PublishedDerivedQuiz:
    """Validate a derived candidate and stage all formal publication rows."""

    candidate_target_id = str(
        candidate_question.get("assessmentTargetId") or ""
    ).strip()
    if candidate_target_id != source.target.id:
        raise _candidate_error(
            "DERIVED_QUIZ_TARGET_MISMATCH",
            "派生题与原题没有测量同一个 Learning Contract 目标",
            sourceAssessmentTargetId=source.target.id,
            candidateAssessmentTargetId=candidate_target_id,
        )
    if not questions_are_substantively_different(
        source.question,
        candidate_question,
    ):
        raise _candidate_error(
            "DERIVED_QUIZ_ITEM_NOT_NOVEL",
            "派生题与原题实质重复",
            sourceAssessmentItemVersionId=source.item.id,
        )
    _validate_answer_authority(candidate_question)

    normalized = dict(candidate_question)
    normalized.pop("id", None)
    normalized.pop("claim_block_indexes", None)
    normalized["itemKey"] = f"{kind}_{quiz_generation}_1"
    normalized["assessmentTargetId"] = source.target.id
    normalized["objective"] = source.target.objective_statement
    normalized["core"] = source.contract_target.required
    normalized["evidenceBlockIds"] = list(source.evidence_block_ids)
    normalized["sourceQuizSetId"] = source.quiz.id
    normalized["sourceAssessmentItemVersionId"] = source.item.id
    normalized["publicationRuleVersion"] = DERIVED_ASSESSMENT_RULE_VERSION
    normalized["equivalenceGroupId"] = equivalence_group_id

    quiz = QuizSet(
        id=uid(f"{kind}_quiz"),
        section_id=source.section.id,
        content_version_id=source.content.id,
        learning_contract_version_id=source.contract.id,
        generation=quiz_generation,
        questions_json="[]",
        publication_status="published",
        schema_version=DERIVED_ASSESSMENT_SCHEMA_VERSION,
    )
    db.add(quiz)
    db.flush()
    published_question = publish_assessment_item_versions(
        db,
        quiz=quiz,
        questions=[normalized],
        evidence_block_ids_by_position=[source.evidence_block_ids],
        uid=uid,
    )[0]

    gate_input = {
        "kind": kind,
        "contractVersionId": source.contract.id,
        "contentVersionId": source.content.id,
        "sourceQuizSetId": source.quiz.id,
        "sourceAssessmentItemVersionId": source.item.id,
        "sourceEvidenceBlockIds": list(source.evidence_block_ids),
        "assessmentTargetId": source.target.id,
        "candidateSignature": question_signature(published_question),
        "ruleVersion": DERIVED_ASSESSMENT_RULE_VERSION,
    }
    snapshot = GovernanceDecisionSnapshot(
        id=uid("governance_decision"),
        decision_scope="quiz_publication",
        content_version_id=source.content.id,
        quiz_set_id=quiz.id,
        learning_contract_version_id=source.contract.id,
        requested_mode="deterministic",
        mode="contract_boundary",
        allowed=True,
        assessment_eligible=True,
        reasons_json="[]",
        rule_version=DERIVED_ASSESSMENT_RULE_VERSION,
        input_hash=_hash(gate_input),
        actor_kind=actor_kind,
        actor_id=actor_id,
        idempotency_key=f"derived-v1:{kind}:{actor_id}",
    )
    db.add(snapshot)
    db.flush()
    return PublishedDerivedQuiz(
        quiz=quiz,
        question=published_question,
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
