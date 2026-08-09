"""Lesson generation v2: spec, deterministic gate, and atomic publisher.

The AI output handled here is a candidate. Only ``publish_lesson_candidate``
creates authoritative lesson and quiz records, and its caller owns the single
database transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai.contracts import GeneratedLessonCandidate
from ..infrastructure.tables import (
    ContentBlockAssessmentTarget,
    ContentBlockClaimAnchor,
    ContentBlockVersion,
    ContentVersion,
    GenerationRun,
    GovernanceDecisionSnapshot,
    LearningContractVersion,
    QuizSet,
    Section,
)
from ..modules.learning.assessment_items import publish_assessment_item_versions


LESSON_GENERATION_PIPELINE_VERSION = "lesson_generation_v2"
LESSON_GENERATION_SCHEMA_VERSION = "generated_lesson_slot_candidate_v4"
LESSON_GENERATION_PROMPT_VERSION = "lesson_generation_slot_prompt_v5"
LESSON_GENERATION_RULE_VERSION = "lesson_candidate_gate_v4"
LESSON_CONTEXT_POLICY_VERSION = "lesson_generation_context_v2"
AI_CONTENT_LABEL_SCHEMA_VERSION = "ai_content_label_v2"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


class LessonSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LessonTargetSpec(LessonSpecModel):
    assessment_target_id: str = Field(alias="assessmentTargetId")
    concept_revision_id: str = Field(default="", alias="conceptRevisionId")
    objective: str
    dimension: str
    target_depth: str = Field(alias="targetDepth")
    required: bool
    verification_policy: str = Field(alias="verificationPolicy")


class NeighborBoundary(LessonSpecModel):
    direction: Literal["previous", "next"]
    section_id: str = Field(alias="sectionId")
    title: str
    question: str
    objectives: list[str] = Field(default_factory=list)


class LessonGenerationSpec(LessonSpecModel):
    """Small, versioned, server-owned input to the one physical model call."""

    pipeline_version: Literal["lesson_generation_v2"] = Field(
        default=LESSON_GENERATION_PIPELINE_VERSION,
        alias="pipelineVersion",
    )
    schema_version: Literal["generated_lesson_slot_candidate_v4"] = Field(
        default=LESSON_GENERATION_SCHEMA_VERSION,
        alias="schemaVersion",
    )
    prompt_version: Literal["lesson_generation_slot_prompt_v5"] = Field(
        default=LESSON_GENERATION_PROMPT_VERSION,
        alias="promptVersion",
    )
    context_policy_version: Literal["lesson_generation_context_v2"] = Field(
        default=LESSON_CONTEXT_POLICY_VERSION,
        alias="contextPolicyVersion",
    )
    generation_mode: Literal["model_only", "rights_grounded", "demo"] = Field(
        alias="generationMode"
    )
    mission: dict[str, Any]
    learner: dict[str, Any]
    section: dict[str, Any]
    learning_contract_version_id: str = Field(alias="learningContractVersionId")
    learning_contract_version: int = Field(alias="learningContractVersion")
    targets: list[LessonTargetSpec] = Field(min_length=1, max_length=8)
    neighbor_boundaries: list[NeighborBoundary] = Field(
        default_factory=list,
        max_length=2,
        alias="neighborBoundaries",
    )
    relevant_mastery: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=8,
        alias="relevantMastery",
    )
    knowledge_context: dict[str, Any] = Field(
        default_factory=lambda: {
            "schemaVersion": "knowledge_context_pack_v1",
            "status": "not_applicable",
            "reason": "standalone_spec_without_curriculum_authority",
            "retrievalRuleVersion": "published_bounded_bfs_v1",
            "budget": {"maxNodes": 12, "maxEdges": 18, "maxHops": 2},
            "nodes": [],
            "edges": [],
            "claims": [],
        },
        alias="knowledgeContext",
    )
    depth_policy: dict[str, Any] = Field(alias="depthPolicy")
    feedback: dict[str, Any] = Field(default_factory=dict)
    rights_asset_version_ids: list[str] = Field(
        default_factory=list,
        max_length=16,
        alias="rightsAssetVersionIds",
    )

    def payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def context_hash(self) -> str:
        return _hash(self.payload())


@dataclass(frozen=True)
class CandidateValidationFailure(Exception):
    code: str
    message: str
    location: dict[str, Any]

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class ValidatedLessonCandidate:
    candidate: GeneratedLessonCandidate
    target_by_id: dict[str, LessonTargetSpec]
    block_by_key: dict[str, Any]


def _reject(code: str, message: str, **location: Any) -> None:
    raise CandidateValidationFailure(code, message, location)


def validate_lesson_candidate(
    spec: LessonGenerationSpec,
    candidate: GeneratedLessonCandidate,
) -> ValidatedLessonCandidate:
    """Pure deterministic gate. It never repairs or guesses a binding."""

    if candidate.decision == "replan_required":
        _reject(
            "PREREQUISITE_GAP_REQUIRES_REPLAN",
            candidate.replan_reason,
            contractVersion=spec.learning_contract_version,
        )

    target_by_id = {item.assessment_target_id: item for item in spec.targets}
    contract_target_ids = set(target_by_id)
    knowledge_ready = spec.knowledge_context.get("status") == "ready"
    allowed_claims = {
        str(item.get("claimVersionId")): item
        for item in spec.knowledge_context.get("claims", [])
        if item.get("claimVersionId")
    }
    block_by_key: dict[str, Any] = {}
    for block in candidate.blocks:
        if block.block_key in block_by_key:
            _reject(
                "CONTENT_BLOCK_KEY_INVALID",
                "正文块局部 Key 重复",
                blockKey=block.block_key,
            )
        block_by_key[block.block_key] = block
        if block.role in {"prerequisite_scaffold", "transition"} and (
            block.assessment_target_ids
        ):
            _reject(
                "CONTENT_ASSESSMENT_TARGET_UNBOUND",
                "支撑性正文块不能声明可考核目标",
                blockKey=block.block_key,
                assessmentTargetIds=block.assessment_target_ids,
                contractVersion=spec.learning_contract_version,
            )
        for target_id in block.assessment_target_ids:
            if target_id not in contract_target_ids:
                _reject(
                    "CONTENT_ASSESSMENT_TARGET_UNBOUND",
                    "正文块引用了当前 Learning Contract 之外的目标",
                    blockKey=block.block_key,
                    assessmentTargetId=target_id,
                    contractVersion=spec.learning_contract_version,
                )
        unknown_claim_ids = set(block.claim_version_ids) - set(allowed_claims)
        if unknown_claim_ids:
            _reject(
                "CONTENT_KNOWLEDGE_CLAIM_UNBOUND",
                "正文块引用了当前 KnowledgeContextPack 之外的知识主张",
                blockKey=block.block_key,
                claimVersionIds=sorted(unknown_claim_ids),
            )
        claim_required = block.role not in {"practice", "transition"}
        if knowledge_ready and claim_required and not block.claim_version_ids:
            _reject(
                "CONTENT_KNOWLEDGE_CLAIM_MISSING",
                "正式课程的事实性正文块缺少显式知识主张绑定",
                blockKey=block.block_key,
                assessmentTargetIds=block.assessment_target_ids,
            )
        for claim_id in block.claim_version_ids:
            scope = allowed_claims[claim_id].get("scope") or {}
            scoped_concepts = set(scope.get("conceptRevisionIds") or [])
            for target_id in block.assessment_target_ids:
                concept_revision_id = target_by_id[target_id].concept_revision_id
                if not concept_revision_id:
                    _reject(
                        "ASSESSMENT_TARGET_CONCEPT_UNBOUND",
                        "正式课程目标缺少冻结概念版本",
                        assessmentTargetId=target_id,
                        blockKey=block.block_key,
                    )
                if scoped_concepts and concept_revision_id not in scoped_concepts:
                    _reject(
                        "CONTENT_KNOWLEDGE_CLAIM_SCOPE_MISMATCH",
                        "正文块的知识主张不支持其冻结概念版本",
                        blockKey=block.block_key,
                        claimVersionId=claim_id,
                        conceptRevisionId=concept_revision_id,
                    )

    feedback_block_id = str(spec.feedback.get("blockId") or "").strip()
    feedback_replacement = candidate.feedback_replacement
    if spec.feedback and not feedback_block_id:
        _reject(
            "FEEDBACK_REPLACEMENT_SOURCE_MISSING",
            "反馈重生成规格缺少原正文块稳定 ID",
        )
    if feedback_block_id:
        if feedback_replacement is None:
            _reject(
                "FEEDBACK_REPLACEMENT_MAPPING_REQUIRED",
                "反馈重生成候选缺少原正文块到新正文块的显式映射",
                sourceBlockId=feedback_block_id,
            )
        if feedback_replacement.source_block_id != feedback_block_id:
            _reject(
                "FEEDBACK_REPLACEMENT_SOURCE_MISMATCH",
                "反馈重生成映射没有引用本次反馈的原正文块",
                expectedSourceBlockId=feedback_block_id,
                actualSourceBlockId=feedback_replacement.source_block_id,
            )
        if feedback_replacement.replacement_block_key not in block_by_key:
            _reject(
                "FEEDBACK_REPLACEMENT_BLOCK_UNBOUND",
                "反馈重生成映射引用了不存在的新正文块",
                sourceBlockId=feedback_block_id,
                replacementBlockKey=feedback_replacement.replacement_block_key,
            )
    elif feedback_replacement is not None:
        _reject(
            "FEEDBACK_REPLACEMENT_UNEXPECTED",
            "非反馈生成候选不能声明段落替换映射",
            sourceBlockId=feedback_replacement.source_block_id,
            replacementBlockKey=feedback_replacement.replacement_block_key,
        )

    item_keys: set[str] = set()
    assessed_target_ids: set[str] = set()
    for question in candidate.questions:
        if question.item_key in item_keys:
            _reject(
                "ASSESSMENT_ITEM_INVALID",
                "测验题局部 Key 重复",
                itemKey=question.item_key,
            )
        item_keys.add(question.item_key)
        target_id = question.assessment_target_id
        if target_id not in contract_target_ids:
            _reject(
                "ASSESSMENT_TARGET_UNBOUND",
                "测验题引用了当前 Learning Contract 之外的目标",
                itemKey=question.item_key,
                assessmentTargetId=target_id,
                contractVersion=spec.learning_contract_version,
            )
        assessed_target_ids.add(target_id)
        for block_key in question.evidence_block_keys:
            block = block_by_key.get(block_key)
            if block is None:
                _reject(
                    "ASSESSMENT_EVIDENCE_BLOCK_UNBOUND",
                    "测验题引用了不存在的正文块",
                    itemKey=question.item_key,
                    blockKey=block_key,
                )
            if target_id not in block.assessment_target_ids:
                _reject(
                    "ASSESSMENT_EVIDENCE_TARGET_MISMATCH",
                    "测验题引用的正文块没有教授同一个目标",
                    itemKey=question.item_key,
                    blockKey=block_key,
                    assessmentTargetId=target_id,
                )

    taught_target_ids = {
        target_id
        for block in candidate.blocks
        for target_id in block.assessment_target_ids
    }
    for target in spec.targets:
        if not target.required:
            continue
        if target.assessment_target_id not in taught_target_ids:
            _reject(
                "REQUIRED_TARGET_NOT_TAUGHT",
                "必需目标没有被任何正文块教授",
                assessmentTargetId=target.assessment_target_id,
                contractVersion=spec.learning_contract_version,
            )
        if target.assessment_target_id not in assessed_target_ids:
            _reject(
                "REQUIRED_TARGET_NOT_ASSESSED",
                "必需目标没有被任何题目测量",
                assessmentTargetId=target.assessment_target_id,
                contractVersion=spec.learning_contract_version,
            )

    return ValidatedLessonCandidate(candidate, target_by_id, block_by_key)


@dataclass(frozen=True)
class PublishedLesson:
    content: ContentVersion
    quiz: QuizSet
    feedback_replacement_block_id: str | None = None


def publish_lesson_candidate(
    db: Session,
    *,
    uid,
    section: Section,
    contract: LearningContractVersion,
    generation_run: GenerationRun,
    spec: LessonGenerationSpec,
    validated: ValidatedLessonCandidate,
    content_version: int,
    quiz_generation: int,
    superseded_content: ContentVersion | None = None,
    superseded_quiz: QuizSet | None = None,
) -> PublishedLesson:
    """Stage all authoritative rows in the caller's single transaction."""

    candidate = validated.candidate
    content = ContentVersion(
        id=uid("content"),
        section_id=section.id,
        learning_contract_version_id=contract.id,
        version=content_version,
        blocks_json="[]",
        sources_json="[]",
        confidence=candidate.confidence,
        publication_status="published",
        schema_version=LESSON_GENERATION_SCHEMA_VERSION,
        prompt_version=LESSON_GENERATION_PROMPT_VERSION,
        generation_mode=spec.generation_mode,
        rights_status=(
            "reviewed" if spec.generation_mode == "rights_grounded" else "not_applicable"
        ),
        factual_status=(
            "claim_grounded"
            if spec.knowledge_context.get("status") == "ready"
            else "unreviewed"
        ),
        ai_generated=True,
        generation_run_id=generation_run.id,
    )
    db.add(content)
    db.flush()

    block_id_by_key: dict[str, str] = {}
    block_index_by_key: dict[str, int] = {}
    block_payloads: list[dict[str, Any]] = []
    pending_block_targets: list[tuple[str, str]] = []
    pending_block_claims: list[tuple[str, str]] = []
    for position, block in enumerate(candidate.blocks):
        block_id = f"block_{content.id}_{position + 1}"
        block_id_by_key[block.block_key] = block_id
        block_index_by_key[block.block_key] = position
        objectives = [
            validated.target_by_id[target_id].objective
            for target_id in block.assessment_target_ids
        ]
        payload = {
            "id": block_id,
            "version": content.version,
            "blockKey": block.block_key,
            "kind": block.kind,
            "role": block.role,
            "relationToAnchor": block.relation_to_anchor,
            "heading": block.heading,
            "content": block.content,
            "source_indexes": [],
            "assessmentTargetIds": block.assessment_target_ids,
            "knowledgeClaimVersionIds": block.claim_version_ids,
            "assessment_objectives": objectives,
        }
        block_payloads.append(payload)
        db.add(
            ContentBlockVersion(
                id=block_id,
                content_version_id=content.id,
                position=position,
                block_version=content.version,
                format_kind=block.kind,
                semantic_role=block.role,
                heading=block.heading,
                content=block.content,
                source_indexes_json="[]",
                factuality_class=(
                    "published_claim_grounded"
                    if block.claim_version_ids
                    else "non_factual_activity"
                    if block.role in {"practice", "transition"}
                    else "model_generated_unreviewed"
                ),
                trust_state=(
                    "claim_grounded" if block.claim_version_ids else "model_synthesis"
                ),
                generation_method=spec.generation_mode,
                assessment_eligible=bool(block.assessment_target_ids),
            )
        )
        for target_id in block.assessment_target_ids:
            pending_block_targets.append((block_id, target_id))
        for claim_id in block.claim_version_ids:
            pending_block_claims.append((block_id, claim_id))
    db.flush()
    for block_id, target_id in pending_block_targets:
        db.add(
            ContentBlockAssessmentTarget(
                id=uid("block_target"),
                content_block_version_id=block_id,
                assessment_target_id=target_id,
                binding_role="teaches",
            )
        )
    for block_id, claim_id in pending_block_claims:
        db.add(
            ContentBlockClaimAnchor(
                id=uid("block_claim_anchor"),
                content_block_version_id=block_id,
                source_claim_version_id=claim_id,
                anchor_role="states",
                locator_json=_dump(
                    {
                        "bindingMode": "explicit_model_claim_id",
                        "knowledgeContextHash": spec.knowledge_context.get(
                            "contextHash", ""
                        ),
                    }
                ),
            )
        )
    content.blocks_json = _dump(block_payloads)
    content.output_hash = _hash({"blocks": block_payloads, "sources": []})
    content.labeling_metadata_json = _dump(
        {
            "schemaVersion": AI_CONTENT_LABEL_SCHEMA_VERSION,
            "generatedContent": True,
            "serviceProvider": "Slow",
            "contentId": content.id,
            "generationRunId": generation_run.id,
            "generationMode": content.generation_mode,
            "rightsStatus": content.rights_status,
            "factualStatus": content.factual_status,
            "model": generation_run.model,
            "pipelineVersion": LESSON_GENERATION_PIPELINE_VERSION,
            "promptVersion": LESSON_GENERATION_PROMPT_VERSION,
            "schemaVersionOfCandidate": LESSON_GENERATION_SCHEMA_VERSION,
            "ruleVersion": LESSON_GENERATION_RULE_VERSION,
            "contextHash": spec.context_hash(),
            "outputHash": content.output_hash,
        }
    )

    quiz = QuizSet(
        id=uid("quiz"),
        section_id=section.id,
        content_version_id=content.id,
        learning_contract_version_id=contract.id,
        generation=quiz_generation,
        questions_json="[]",
        publication_status="published",
        schema_version=LESSON_GENERATION_SCHEMA_VERSION,
    )
    db.add(quiz)
    db.flush()

    question_payloads: list[dict[str, Any]] = []
    evidence_block_ids_by_position: list[list[str]] = []
    for position, question in enumerate(candidate.questions):
        target = validated.target_by_id[question.assessment_target_id]
        item_id = uid("assessment_item")
        evidence_block_ids = [
            block_id_by_key[key] for key in question.evidence_block_keys
        ]
        payload = {
            "id": item_id,
            "itemKey": question.item_key,
            "assessmentTargetId": question.assessment_target_id,
            "objective": target.objective,
            "core": target.required,
            "evidenceBlockIds": evidence_block_ids,
            "evidenceBlockKeys": question.evidence_block_keys,
            "prompt": question.prompt,
            "options": question.options,
            "correct": question.correct,
            "explanation": question.explanation,
            "difficulty": question.difficulty,
            "claim_block_indexes": [
                block_index_by_key[key] for key in question.evidence_block_keys
            ],
            "equivalenceGroupId": (
                f"{question.assessment_target_id}:contract:"
                f"{target.verification_policy}:slot:{position}"
            ),
        }
        question_payloads.append(payload)
        evidence_block_ids_by_position.append(evidence_block_ids)
    question_payloads = publish_assessment_item_versions(
        db,
        quiz=quiz,
        questions=question_payloads,
        evidence_block_ids_by_position=evidence_block_ids_by_position,
        uid=uid,
    )

    if superseded_content:
        superseded_content.publication_status = "superseded"
    if superseded_quiz:
        superseded_quiz.publication_status = "superseded"

    gate_input = {
        "contractVersionId": contract.id,
        "contentVersionId": content.id,
        "quizSetId": quiz.id,
        "blockBindings": [
            {
                "blockKey": block.block_key,
                "assessmentTargetIds": block.assessment_target_ids,
            }
            for block in candidate.blocks
        ],
        "questionBindings": [
            {
                "itemKey": item.item_key,
                "assessmentTargetId": item.assessment_target_id,
                "evidenceBlockKeys": item.evidence_block_keys,
            }
            for item in candidate.questions
        ],
        "feedbackReplacement": (
            {
                "sourceBlockId": candidate.feedback_replacement.source_block_id,
                "replacementBlockKey": (
                    candidate.feedback_replacement.replacement_block_key
                ),
            }
            if candidate.feedback_replacement
            else None
        ),
    }
    for scope, quiz_id in (("content_publication", None), ("quiz_publication", quiz.id)):
        db.add(
            GovernanceDecisionSnapshot(
                id=uid("governance_decision"),
                decision_scope=scope,
                content_version_id=content.id,
                quiz_set_id=quiz_id,
                learning_contract_version_id=contract.id,
                requested_mode="deterministic",
                mode=(
                    "published_knowledge_claims"
                    if spec.knowledge_context.get("status") == "ready"
                    else "contract_boundary"
                ),
                allowed=True,
                assessment_eligible=True,
                reasons_json="[]",
                rule_version=LESSON_GENERATION_RULE_VERSION,
                input_hash=_hash({**gate_input, "scope": scope}),
                actor_kind="generation_attempt",
                actor_id=generation_run.id,
                idempotency_key=(
                    f"lesson-v2:content:{content.id}"
                    if quiz_id is None
                    else f"lesson-v2:quiz:{quiz.id}"
                ),
            )
        )

    db.flush()
    replacement_block_id = (
        block_id_by_key[candidate.feedback_replacement.replacement_block_key]
        if candidate.feedback_replacement
        else None
    )
    return PublishedLesson(
        content=content,
        quiz=quiz,
        feedback_replacement_block_id=replacement_block_id,
    )
