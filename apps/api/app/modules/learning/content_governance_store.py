"""Persistence adapter for the deterministic M2 content-governance rules.

The current lesson schema can name source indexes, but it cannot prove that a
source semantically supports a paragraph.  This adapter therefore normalizes
the lineage and opens explicit gaps without ever upgrading URL reachability to
claim support.  A later claim verifier can append verified bindings and replay
the same pure publication rule.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ContentBlockClaimAnchor,
    ContentBlockVersion,
    ContentVersion,
    GovernanceDecisionSnapshot,
    KnowledgeGap,
    KnowledgeGapEvent,
    LearningContractAssessmentTarget,
    QuizSet,
    SourceClaim,
    SourceClaimBinding,
    SourceClaimVersion,
    SourceVersion,
    now,
)
from .content_governance import (
    CONTENT_GOVERNANCE_RULE_VERSION,
    ContentBlockInput,
    ContentGovernanceInput,
    KnowledgeGapInput,
    QuestionDependencyInput,
    QuizGovernanceInput,
    SourceClaimBindingInput,
    SourceClaimInput,
    evaluate_content_publication,
    evaluate_quiz_publication,
)


NORMALIZATION_RULE_VERSION = "content_lineage_v1"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _hash(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()


def _id(prefix: str, *parts) -> str:
    return f"{prefix}_{_hash(*parts)[:32]}"


def _claim_kind(block: dict) -> str | None:
    role = str(block.get("role", ""))
    if role == "conclusion":
        return "core_conclusion"
    if role == "boundary":
        return "boundary"
    if block.get("assessmentEligible") or block.get("assessment_eligible"):
        return "assessable_fact"
    return None


def persist_generated_governance(
    db: Session,
    *,
    content: ContentVersion,
    quiz: QuizSet,
    source_verification: list[dict],
    actor_id: str,
) -> dict:
    """Normalize one generated pair and persist replayable publication facts.

    This function is idempotent for a content/quiz pair.  Candidate bindings
    intentionally use ``reachability_only`` and can never satisfy the pure
    formal-publication rule.
    """

    existing = db.scalar(
        select(GovernanceDecisionSnapshot).where(
            GovernanceDecisionSnapshot.decision_scope == "quiz_publication",
            GovernanceDecisionSnapshot.idempotency_key == f"quiz:{quiz.id}",
        )
    )
    if existing:
        return _decision_view(existing)

    sources = _load(content.sources_json, [])
    blocks = _load(content.blocks_json, [])
    questions = _load(quiz.questions_json, [])
    reports = {
        str(item.get("url", "")): item
        for item in source_verification
        if isinstance(item, dict)
    }
    source_rows: dict[int, SourceVersion] = {}
    for position, source in enumerate(sources):
        if not isinstance(source, dict):
            continue
        report = reports.get(str(source.get("url", "")), {})
        row = SourceVersion(
            id=_id("source_version", content.id, position),
            content_version_id=content.id,
            position=position,
            title=str(source.get("title", "")),
            url=str(source.get("url", "")),
            source_kind=str(source.get("kind", "unknown")),
            version_label=str(source.get("version", "")),
            provenance_mode="native_m2",
            reachability_status=str(report.get("verificationStatus", "unknown")),
            verification_report_json=_dump(report),
        )
        db.add(row)
        source_rows[position] = row
    db.flush()

    target_ids = tuple(
        db.scalars(
            select(LearningContractAssessmentTarget.assessment_target_id).where(
                LearningContractAssessmentTarget.contract_version_id
                == quiz.learning_contract_version_id
            )
        ).all()
    )
    block_inputs: list[ContentBlockInput] = []
    claim_inputs: list[SourceClaimInput] = []
    binding_inputs: list[SourceClaimBindingInput] = []
    gap_inputs: list[KnowledgeGapInput] = []
    claim_ids: list[str] = []

    for position, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_id = str(block.get("id") or _id("block", content.id, position))
        role = str(block.get("role") or "transition")
        block_row = ContentBlockVersion(
            id=block_id,
            content_version_id=content.id,
            position=position,
            block_version=int(block.get("version") or content.version),
            format_kind=str(block.get("kind") or "text"),
            semantic_role=role,
            heading=str(block.get("heading") or ""),
            content=str(block.get("content") or ""),
            source_indexes_json=_dump(block.get("source_indexes", [])),
            factuality_class=str(block.get("factualityClass") or "unspecified"),
            trust_state="model_synthesis",
            generation_method="ai_generated",
            assessment_eligible=False,
        )
        db.add(block_row)
        # The legacy-compatible paragraph schema has no objective-local anchor.
        # Record the contract-wide inference transparently; do not invent proof.
        taught_targets = target_ids if role in {"conclusion", "mechanism", "boundary"} else ()
        block_inputs.append(
            ContentBlockInput(
                id=block_id,
                role=role,
                assessment_target_ids=taught_targets,
                assessment_eligible=False,
            )
        )
        kind = _claim_kind(block)
        if not kind:
            continue
        stable_key = _hash("generated_block_claim", content.id, block_id, kind)
        claim = SourceClaim(
            id=_id("source_claim", stable_key),
            stable_key=stable_key,
            status="active",
        )
        claim_version = SourceClaimVersion(
            id=_id("source_claim_version", claim.id, 1),
            source_claim_id=claim.id,
            version=1,
            statement=str(block.get("content") or block.get("heading") or block_id),
            claim_kind=kind,
            scope_json=_dump(
                {
                    "contentVersionId": content.id,
                    "contentBlockVersionId": block_id,
                    "targetMapping": "contract_wide_inference",
                }
            ),
            strict=True,
            trust_state="unverified",
            generation_method="ai_generated",
            status="candidate",
        )
        db.add_all([claim, claim_version])
        db.flush()
        db.add(
            ContentBlockClaimAnchor(
                id=_id("block_claim_anchor", block_id, claim_version.id),
                content_block_version_id=block_id,
                source_claim_version_id=claim_version.id,
                anchor_role="states",
                locator_json=_dump({"kind": "whole_block"}),
            )
        )
        claim_ids.append(claim_version.id)
        claim_inputs.append(
            SourceClaimInput(
                id=claim_version.id,
                block_id=block_id,
                kind=kind,
                explicitly_assessable=kind == "assessable_fact",
            )
        )
        for source_index in block.get("source_indexes", []):
            source_row = source_rows.get(source_index)
            if not source_row:
                continue
            locator = {"sourceIndex": source_index, "kind": "block_reference"}
            locator_json = _dump(locator)
            candidate = SourceClaimBinding(
                id=_id("claim_binding", claim_version.id, source_row.id, locator_json),
                source_claim_version_id=claim_version.id,
                source_version_id=source_row.id,
                locator_type="block_reference",
                locator_json=locator_json,
                locator_hash=_hash(locator_json),
                excerpt_text="",
                excerpt_hash="",
                support_type="candidate_support",
                verification_mode="reachability_only",
                verification_status="unverified",
                verification_rule_version=NORMALIZATION_RULE_VERSION,
                report_json=_dump(
                    {
                        "sourceReachability": source_row.reachability_status,
                        "warning": "source_reachability_is_not_claim_support",
                    }
                ),
                verified_at=None,
            )
            db.add(candidate)
            binding_inputs.append(
                SourceClaimBindingInput(
                    claim_id=claim_version.id,
                    source_version_id=source_row.id,
                    support_type="candidate_support",
                    verification_status="unverified",
                    locator=locator_json,
                )
            )
        gap = KnowledgeGap(
            id=_id("knowledge_gap", claim_version.id, "unsupported_claim"),
            gap_type="unsupported_claim",
            severity="blocking",
            subject_kind="source_claim_version",
            source_claim_version_id=claim_version.id,
            content_version_id=content.id,
            content_block_version_id=block_id,
            detector_kind="system_generation",
            detector_rule_version=NORMALIZATION_RULE_VERSION,
            details_json=_dump(
                {
                    "reason": "source indexes and reachability do not prove claim support",
                    "candidateBindingCount": len(block.get("source_indexes", [])),
                }
            ),
        )
        db.add(gap)
        db.flush()
        db.add(
            KnowledgeGapEvent(
                id=_id("knowledge_gap_event", gap.id, "opened"),
                knowledge_gap_id=gap.id,
                event_type="opened",
                actor_kind="system_generation",
                actor_id=actor_id,
                rationale="strict claim requires claim-level verification",
                evidence_json="{}",
                rule_version=NORMALIZATION_RULE_VERSION,
                idempotency_key=f"opened:{gap.id}",
            )
        )
        gap_inputs.append(
            KnowledgeGapInput(
                id=gap.id,
                gap_type=gap.gap_type,
                severity=gap.severity,
                subject_id=claim_version.id,
            )
        )

    content_input = ContentGovernanceInput(
        blocks=tuple(block_inputs),
        claims=tuple(claim_inputs),
        claim_bindings=tuple(binding_inputs),
        knowledge_gaps=tuple(gap_inputs),
        requested_mode="formal",
    )
    content_decision = evaluate_content_publication(content_input)
    dependency_claims = tuple(
        claim.id
        for claim in claim_inputs
        if claim.kind == "core_conclusion"
    ) or tuple(claim_ids)
    quiz_input = QuizGovernanceInput(
        content=content_input,
        questions=tuple(
            QuestionDependencyInput(
                id=f"{quiz.id}:{index}",
                primary_assessment_target_id=str(question.get("assessmentTargetId", "")),
                claim_ids=dependency_claims,
            )
            for index, question in enumerate(questions)
            if isinstance(question, dict)
        ),
        contract_assessment_target_ids=frozenset(target_ids),
    )
    quiz_decision = evaluate_quiz_publication(quiz_input)
    content_snapshot = _snapshot(
        content=content,
        quiz=None,
        decision_scope="content_publication",
        decision=content_decision,
        actor_id=actor_id,
        input_payload={"content": _content_input_payload(content_input)},
        idempotency_key=f"content:{content.id}",
    )
    quiz_snapshot = _snapshot(
        content=content,
        quiz=quiz,
        decision_scope="quiz_publication",
        decision=quiz_decision,
        actor_id=actor_id,
        input_payload={
            "content": _content_input_payload(content_input),
            "questions": [
                {
                    "id": item.id,
                    "assessmentTargetId": item.primary_assessment_target_id,
                    "claimIds": list(item.claim_ids),
                }
                for item in quiz_input.questions
            ],
            "contractAssessmentTargetIds": sorted(target_ids),
        },
        idempotency_key=f"quiz:{quiz.id}",
    )
    db.add_all([content_snapshot, quiz_snapshot])
    db.flush()
    return _decision_view(quiz_snapshot)


def _content_input_payload(value: ContentGovernanceInput) -> dict:
    return {
        "blocks": [item.__dict__ for item in value.blocks],
        "claims": [item.__dict__ for item in value.claims],
        "claimBindings": [item.__dict__ for item in value.claim_bindings],
        "knowledgeGaps": [item.__dict__ for item in value.knowledge_gaps],
        "requestedMode": value.requested_mode,
    }


def _snapshot(
    *,
    content,
    quiz,
    decision_scope,
    decision,
    actor_id,
    input_payload,
    idempotency_key,
) -> GovernanceDecisionSnapshot:
    return GovernanceDecisionSnapshot(
        id=_id("governance_decision", decision_scope, idempotency_key),
        decision_scope=decision_scope,
        content_version_id=content.id,
        quiz_set_id=quiz.id if quiz else None,
        learning_contract_version_id=content.learning_contract_version_id,
        requested_mode=decision.requested_mode,
        mode=decision.mode,
        allowed=decision.allowed,
        assessment_eligible=decision.assessment_eligible,
        reasons_json=_dump(decision.as_dict()["reasons"]),
        rule_version=CONTENT_GOVERNANCE_RULE_VERSION,
        input_hash=_hash(_dump(input_payload)),
        actor_kind="generation_run",
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        created_at=now(),
    )


def _decision_view(snapshot: GovernanceDecisionSnapshot) -> dict:
    return {
        "decisionId": snapshot.id,
        "scope": snapshot.decision_scope,
        "requestedMode": snapshot.requested_mode,
        "mode": snapshot.mode,
        "allowed": snapshot.allowed,
        "assessmentEligible": snapshot.assessment_eligible,
        "reasons": _load(snapshot.reasons_json, []),
        "ruleVersion": snapshot.rule_version,
    }


def governance_view_for_quiz(db: Session, quiz_id: str | None) -> dict | None:
    if not quiz_id:
        return None
    snapshot = db.scalar(
        select(GovernanceDecisionSnapshot)
        .where(
            GovernanceDecisionSnapshot.decision_scope == "quiz_publication",
            GovernanceDecisionSnapshot.quiz_set_id == quiz_id,
        )
        .order_by(GovernanceDecisionSnapshot.created_at.desc())
    )
    return _decision_view(snapshot) if snapshot else None


def claim_verification_candidates(db: Session, *, content_version_id: str) -> list[dict]:
    """Return explicit verifier input; reachability facts are intentionally absent."""

    blocks = db.scalars(
        select(ContentBlockVersion).where(
            ContentBlockVersion.content_version_id == content_version_id
        )
    ).all()
    anchors = db.scalars(
        select(ContentBlockClaimAnchor).where(
            ContentBlockClaimAnchor.content_block_version_id.in_(
                [item.id for item in blocks]
            )
        )
    ).all() if blocks else []
    claims = {
        item.id: item
        for item in db.scalars(
            select(SourceClaimVersion).where(
                SourceClaimVersion.id.in_(
                    [item.source_claim_version_id for item in anchors]
                )
            )
        ).all()
    } if anchors else {}
    block_by_id = {item.id: item for item in blocks}
    sources = {
        item.position: item
        for item in db.scalars(
            select(SourceVersion).where(
                SourceVersion.content_version_id == content_version_id
            )
        ).all()
    }
    result = []
    for anchor in anchors:
        block = block_by_id[anchor.content_block_version_id]
        claim = claims[anchor.source_claim_version_id]
        for index in _load(block.source_indexes_json, []):
            source = sources.get(index)
            if source:
                result.append(
                    {
                        "sourceClaimVersionId": claim.id,
                        "sourceVersionId": source.id,
                        "statement": claim.statement,
                        "claimKind": claim.claim_kind,
                        "contentBlockVersionId": block.id,
                        "sourceTitle": source.title,
                        "sourceUrl": source.url,
                    }
                )
    return result


def record_verified_claim_binding(
    db: Session,
    *,
    source_claim_version_id: str,
    source_version_id: str,
    locator_type: str,
    locator: dict,
    excerpt_text: str,
    support_type: str,
    verification_mode: str,
    verification_rule_version: str,
    report: dict,
    actor_id: str,
) -> SourceClaimBinding:
    """The only M2 writer that may create a verified support binding.

    Reachability-oriented modes are rejected even if their report says
    ``verified``.  The caller must be a dedicated claim verifier and provide an
    actual locator, excerpt, mode, and versioned report.
    """

    claim = db.get(SourceClaimVersion, source_claim_version_id)
    source = db.get(SourceVersion, source_version_id)
    if not claim or not source:
        raise AppError(
            "主张或来源版本不存在",
            code="CLAIM_VERIFICATION_SUBJECT_MISSING",
            status=404,
        )
    anchors = db.scalars(
        select(ContentBlockClaimAnchor).where(
            ContentBlockClaimAnchor.source_claim_version_id == claim.id
        )
    ).all()
    block_ids = [item.content_block_version_id for item in anchors]
    blocks = db.scalars(
        select(ContentBlockVersion).where(ContentBlockVersion.id.in_(block_ids))
    ).all() if block_ids else []
    if not blocks or any(
        block.content_version_id != source.content_version_id for block in blocks
    ):
        raise AppError(
            "核验来源不属于该主张锚定的正文版本",
            code="CLAIM_VERIFICATION_LINEAGE_MISMATCH",
            status=409,
        )
    if support_type not in {"supports", "defines"}:
        raise AppError(
            "核验关系必须是 supports 或 defines",
            code="CLAIM_VERIFICATION_SUPPORT_TYPE_INVALID",
            status=400,
        )
    if (
        not locator_type.strip()
        or not locator
        or not excerpt_text.strip()
        or not verification_rule_version.strip()
        or verification_mode in {"reachability_only", "legacy_migration"}
    ):
        raise AppError(
            "主张核验必须携带定位、摘录、专用核验方式和规则版本",
            code="CLAIM_VERIFICATION_EVIDENCE_INCOMPLETE",
            status=400,
        )
    locator_json = _dump(locator)
    locator_hash = _hash(locator_json)
    existing = db.scalar(
        select(SourceClaimBinding).where(
            SourceClaimBinding.source_claim_version_id == claim.id,
            SourceClaimBinding.source_version_id == source.id,
            SourceClaimBinding.locator_hash == locator_hash,
        )
    )
    if existing:
        if (
            existing.excerpt_hash != _hash(excerpt_text.strip())
            or existing.verification_mode != verification_mode
            or existing.verification_rule_version != verification_rule_version
        ):
            raise AppError(
                "同一主张核验定位已被不同证据占用",
                code="CLAIM_VERIFICATION_IDEMPOTENCY_CONFLICT",
                status=409,
            )
        return existing
    binding = SourceClaimBinding(
        id=_id("claim_binding_verified", claim.id, source.id, locator_hash),
        source_claim_version_id=claim.id,
        source_version_id=source.id,
        locator_type=locator_type,
        locator_json=locator_json,
        locator_hash=locator_hash,
        excerpt_text=excerpt_text.strip(),
        excerpt_hash=_hash(excerpt_text.strip()),
        support_type=support_type,
        verification_mode=verification_mode,
        verification_status="verified",
        verification_rule_version=verification_rule_version,
        report_json=_dump(report),
        verified_at=now(),
    )
    db.add(binding)
    db.flush()
    gaps = db.scalars(
        select(KnowledgeGap).where(
            KnowledgeGap.source_claim_version_id == claim.id,
            KnowledgeGap.gap_type == "unsupported_claim",
        )
    ).all()
    for gap in gaps:
        key = f"resolved:{binding.id}"
        if not db.scalar(
            select(KnowledgeGapEvent).where(
                KnowledgeGapEvent.knowledge_gap_id == gap.id,
                KnowledgeGapEvent.idempotency_key == key,
            )
        ):
            db.add(
                KnowledgeGapEvent(
                    id=_id("knowledge_gap_event", gap.id, key),
                    knowledge_gap_id=gap.id,
                    event_type="resolved",
                    actor_kind="claim_verifier",
                    actor_id=actor_id,
                    rationale="claim-level support verified",
                    evidence_json=_dump({"sourceClaimBindingId": binding.id}),
                    rule_version=verification_rule_version,
                    idempotency_key=key,
                )
            )
    db.flush()
    return binding


def reevaluate_generated_governance(
    db: Session,
    *,
    quiz_id: str,
    actor_id: str,
) -> dict:
    """Replay formal publication after claim-verification or gap events."""

    quiz = db.get(QuizSet, quiz_id)
    content = db.get(ContentVersion, quiz.content_version_id) if quiz else None
    if not quiz or not content:
        raise AppError("题集或正文不存在", code="GOVERNANCE_SUBJECT_MISSING", status=404)
    block_rows = db.scalars(
        select(ContentBlockVersion)
        .where(ContentBlockVersion.content_version_id == content.id)
        .order_by(ContentBlockVersion.position)
    ).all()
    anchors = db.scalars(
        select(ContentBlockClaimAnchor).where(
            ContentBlockClaimAnchor.content_block_version_id.in_(
                [item.id for item in block_rows]
            )
        )
    ).all() if block_rows else []
    claim_rows = {
        item.id: item
        for item in db.scalars(
            select(SourceClaimVersion).where(
                SourceClaimVersion.id.in_(
                    [item.source_claim_version_id for item in anchors]
                )
            )
        ).all()
    } if anchors else {}
    claim_block = {
        item.source_claim_version_id: item.content_block_version_id
        for item in anchors
    }
    binding_rows = db.scalars(
        select(SourceClaimBinding).where(
            SourceClaimBinding.source_claim_version_id.in_(list(claim_rows))
        )
    ).all() if claim_rows else []
    gap_rows = db.scalars(
        select(KnowledgeGap).where(KnowledgeGap.content_version_id == content.id)
    ).all()
    gap_events = db.scalars(
        select(KnowledgeGapEvent)
        .where(
            KnowledgeGapEvent.knowledge_gap_id.in_([item.id for item in gap_rows])
        )
        .order_by(KnowledgeGapEvent.sequence)
    ).all() if gap_rows else []
    latest_gap_event = {}
    for event in gap_events:
        latest_gap_event[event.knowledge_gap_id] = event.event_type
    target_ids = tuple(
        db.scalars(
            select(LearningContractAssessmentTarget.assessment_target_id).where(
                LearningContractAssessmentTarget.contract_version_id
                == quiz.learning_contract_version_id
            )
        ).all()
    )
    block_inputs = tuple(
        ContentBlockInput(
            id=item.id,
            role=item.semantic_role,
            assessment_target_ids=(
                target_ids
                if item.semantic_role in {"conclusion", "mechanism", "boundary"}
                else ()
            ),
            assessment_eligible=item.assessment_eligible,
        )
        for item in block_rows
    )
    claim_inputs = tuple(
        SourceClaimInput(
            id=item.id,
            block_id=claim_block[item.id],
            kind=item.claim_kind,
            explicitly_assessable=item.claim_kind == "assessable_fact",
        )
        for item in claim_rows.values()
    )
    verified_by_claim: dict[str, list[SourceClaimBinding]] = {}
    for item in binding_rows:
        if item.verification_status == "verified":
            verified_by_claim.setdefault(item.source_claim_version_id, []).append(item)
    binding_inputs = tuple(
        SourceClaimBindingInput(
            claim_id=item.source_claim_version_id,
            source_version_id=item.source_version_id,
            support_type=item.support_type,
            verification_status=(
                "cross_source"
                if len(verified_by_claim.get(item.source_claim_version_id, [])) >= 2
                else item.verification_status
            ),
            locator=item.locator_json,
        )
        for item in binding_rows
    )
    gap_inputs = tuple(
        KnowledgeGapInput(
            id=item.id,
            gap_type=item.gap_type,
            severity=item.severity,
            status=(
                "resolved"
                if latest_gap_event.get(item.id) in {"resolved", "closed"}
                else "open"
            ),
            subject_id=item.source_claim_version_id or item.content_block_version_id or "",
        )
        for item in gap_rows
    )
    content_input = ContentGovernanceInput(
        blocks=block_inputs,
        claims=claim_inputs,
        claim_bindings=binding_inputs,
        knowledge_gaps=gap_inputs,
        requested_mode="formal",
    )
    conclusion_claims = tuple(
        item.id for item in claim_inputs if item.kind == "core_conclusion"
    ) or tuple(item.id for item in claim_inputs)
    quiz_input = QuizGovernanceInput(
        content=content_input,
        questions=tuple(
            QuestionDependencyInput(
                id=f"{quiz.id}:{index}",
                primary_assessment_target_id=str(question.get("assessmentTargetId", "")),
                claim_ids=conclusion_claims,
            )
            for index, question in enumerate(_load(quiz.questions_json, []))
        ),
        contract_assessment_target_ids=frozenset(target_ids),
    )
    decision = evaluate_quiz_publication(quiz_input)
    input_payload = {
        "content": _content_input_payload(content_input),
        "questions": [item.__dict__ for item in quiz_input.questions],
        "contractAssessmentTargetIds": sorted(target_ids),
    }
    input_hash = _hash(_dump(input_payload))
    snapshot = _snapshot(
        content=content,
        quiz=quiz,
        decision_scope="quiz_publication",
        decision=decision,
        actor_id=actor_id,
        input_payload=input_payload,
        idempotency_key=f"quiz:{quiz.id}:replay:{input_hash}",
    )
    existing = db.scalar(
        select(GovernanceDecisionSnapshot).where(
            GovernanceDecisionSnapshot.decision_scope == "quiz_publication",
            GovernanceDecisionSnapshot.idempotency_key == snapshot.idempotency_key,
        )
    )
    if existing:
        return _decision_view(existing)
    db.add(snapshot)
    db.flush()
    return _decision_view(snapshot)
