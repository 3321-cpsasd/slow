"""Deterministic M2 publication gates for lesson content and assessments.

This module deliberately has no persistence dependency. Database adapters can map
versioned content blocks, claims, bindings, gaps, and questions into these frozen
inputs, then persist the returned decision as an audit fact.
"""

from dataclasses import dataclass, field
from typing import Literal


CONTENT_GOVERNANCE_RULE_VERSION = "content_governance_v2"

ContentRole = str
PublicationMode = Literal["formal", "experimental"]

STRICT_CLAIM_KINDS = frozenset(
    {"core_conclusion", "boundary", "assessable_fact"}
)
SUPPORTING_RELATIONS = frozenset({"supports", "defines"})
VERIFIED_SUPPORT_STATUSES = frozenset({"verified", "cross_source"})
CLOSED_GAP_STATUSES = frozenset({"resolved", "closed"})


@dataclass(frozen=True)
class ContentBlockInput:
    id: str
    role: ContentRole
    assessment_target_ids: tuple[str, ...] = ()
    assessment_eligible: bool = False
    factuality_class: str = "unspecified"
    case_kind: str = ""


@dataclass(frozen=True)
class SourceClaimInput:
    id: str
    block_id: str
    kind: str = "teaching_synthesis"
    explicitly_assessable: bool = False


@dataclass(frozen=True)
class SourceClaimBindingInput:
    claim_id: str
    source_version_id: str
    support_type: str
    verification_status: str
    locator: str = ""


@dataclass(frozen=True)
class KnowledgeGapInput:
    id: str
    gap_type: str
    severity: str
    status: str = "open"
    experimental_allowed: bool = True
    subject_id: str = ""


@dataclass(frozen=True)
class QuestionDependencyInput:
    id: str
    primary_assessment_target_id: str
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class GovernanceReason:
    code: str
    message: str
    severity: Literal["blocking", "warning"] = "blocking"
    subject_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GovernanceDecision:
    allowed: bool
    mode: Literal["formal", "experimental", "rejected"]
    requested_mode: PublicationMode
    reasons: tuple[GovernanceReason, ...]
    rule_version: str = CONTENT_GOVERNANCE_RULE_VERSION
    assessment_eligible: bool = False

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "requestedMode": self.requested_mode,
            "reasons": [
                {
                    "code": reason.code,
                    "message": reason.message,
                    "severity": reason.severity,
                    "subjectIds": list(reason.subject_ids),
                }
                for reason in self.reasons
            ],
            "ruleVersion": self.rule_version,
            "assessmentEligible": self.assessment_eligible,
        }


@dataclass(frozen=True)
class ContentGovernanceInput:
    blocks: tuple[ContentBlockInput, ...]
    claims: tuple[SourceClaimInput, ...] = ()
    claim_bindings: tuple[SourceClaimBindingInput, ...] = ()
    knowledge_gaps: tuple[KnowledgeGapInput, ...] = ()
    requested_mode: PublicationMode = "formal"
    explicit_experimental_consent: bool = False


@dataclass(frozen=True)
class QuizGovernanceInput:
    content: ContentGovernanceInput
    questions: tuple[QuestionDependencyInput, ...]
    contract_assessment_target_ids: frozenset[str] = field(
        default_factory=frozenset
    )


def _strict_claim(
    claim: SourceClaimInput,
    blocks_by_id: dict[str, ContentBlockInput],
) -> bool:
    block = blocks_by_id.get(claim.block_id)
    return bool(
        claim.explicitly_assessable
        or claim.kind in STRICT_CLAIM_KINDS
        or (block and block.assessment_eligible)
        or (block and block.case_kind in {"empirical_case", "primary_source_case"})
    )


def _claim_has_verified_support(
    claim_id: str,
    bindings_by_claim: dict[str, list[SourceClaimBindingInput]],
) -> bool:
    return any(
        binding.support_type in SUPPORTING_RELATIONS
        and binding.verification_status in VERIFIED_SUPPORT_STATUSES
        and bool(binding.locator.strip())
        for binding in bindings_by_claim.get(claim_id, [])
    )


def _indexes(candidate: ContentGovernanceInput):
    blocks_by_id = {block.id: block for block in candidate.blocks}
    claims_by_id = {claim.id: claim for claim in candidate.claims}
    claims_by_block: dict[str, list[SourceClaimInput]] = {}
    for claim in candidate.claims:
        claims_by_block.setdefault(claim.block_id, []).append(claim)
    bindings_by_claim: dict[str, list[SourceClaimBindingInput]] = {}
    for binding in candidate.claim_bindings:
        bindings_by_claim.setdefault(binding.claim_id, []).append(binding)
    return blocks_by_id, claims_by_id, claims_by_block, bindings_by_claim


def _content_reasons(candidate: ContentGovernanceInput) -> list[GovernanceReason]:
    blocks_by_id, claims_by_id, claims_by_block, bindings_by_claim = _indexes(
        candidate
    )
    reasons: list[GovernanceReason] = []

    duplicate_block_ids = _duplicates(block.id for block in candidate.blocks)
    if duplicate_block_ids:
        reasons.append(
            GovernanceReason(
                code="DUPLICATE_CONTENT_BLOCK_ID",
                message="内容块 ID 必须在同一正文版本内唯一。",
                subject_ids=tuple(sorted(duplicate_block_ids)),
            )
        )

    duplicate_claim_ids = _duplicates(claim.id for claim in candidate.claims)
    if duplicate_claim_ids:
        reasons.append(
            GovernanceReason(
                code="DUPLICATE_SOURCE_CLAIM_ID",
                message="原子主张 ID 必须唯一。",
                subject_ids=tuple(sorted(duplicate_claim_ids)),
            )
        )

    orphan_claims = sorted(
        claim.id for claim in candidate.claims if claim.block_id not in blocks_by_id
    )
    if orphan_claims:
        reasons.append(
            GovernanceReason(
                code="SOURCE_CLAIM_NOT_ANCHORED",
                message="原子主张必须锚定到当前正文版本的内容块。",
                subject_ids=tuple(orphan_claims),
            )
        )

    strict_blocks_without_claim = []
    assessable_blocks_without_claim = []
    for block in candidate.blocks:
        block_claims = claims_by_block.get(block.id, [])
        strict_claims = [
            claim for claim in block_claims if _strict_claim(claim, blocks_by_id)
        ]
        requires_claim = (
            block.assessment_eligible
            or block.case_kind in {"empirical_case", "primary_source_case"}
        )
        if requires_claim and not strict_claims:
            strict_blocks_without_claim.append(block.id)
        if block.assessment_eligible and not any(
            claim.explicitly_assessable or claim.kind == "assessable_fact"
            for claim in block_claims
        ):
            assessable_blocks_without_claim.append(block.id)
    if strict_blocks_without_claim:
        reasons.append(
            GovernanceReason(
                code="STRICT_CLAIM_MISSING",
                message="可考核正文和真实案例必须声明可追溯的原子主张。",
                subject_ids=tuple(sorted(strict_blocks_without_claim)),
            )
        )
    if assessable_blocks_without_claim:
        reasons.append(
            GovernanceReason(
                code="ASSESSABLE_CLAIM_MISSING",
                message="允许参与验证的内容块必须明确声明可考核原子主张。",
                subject_ids=tuple(sorted(assessable_blocks_without_claim)),
            )
        )

    unsupported_strict_claims = sorted(
        claim.id
        for claim in claims_by_id.values()
        if _strict_claim(claim, blocks_by_id)
        and not _claim_has_verified_support(claim.id, bindings_by_claim)
    )
    if unsupported_strict_claims:
        reasons.append(
            GovernanceReason(
                code="STRICT_CLAIM_UNSUPPORTED",
                message="严格主张缺少已核验且可定位的来源支持。",
                subject_ids=tuple(unsupported_strict_claims),
            )
        )

    blocking_gaps = [
        gap
        for gap in candidate.knowledge_gaps
        if gap.severity == "blocking" and gap.status not in CLOSED_GAP_STATUSES
    ]
    if blocking_gaps:
        reasons.append(
            GovernanceReason(
                code="BLOCKING_KNOWLEDGE_GAP",
                message="存在尚未解决的阻断型知识缺口。",
                subject_ids=tuple(sorted(gap.id for gap in blocking_gaps)),
            )
        )
    return reasons


def evaluate_content_publication(
    candidate: ContentGovernanceInput,
) -> GovernanceDecision:
    reasons = _content_reasons(candidate)
    structural_codes = {
        "DUPLICATE_CONTENT_BLOCK_ID",
        "SEMANTIC_CLOSURE_INCOMPLETE",
        "DUPLICATE_SOURCE_CLAIM_ID",
        "SOURCE_CLAIM_NOT_ANCHORED",
    }
    structural_reasons = [reason for reason in reasons if reason.code in structural_codes]
    if structural_reasons:
        return _rejected(candidate.requested_mode, reasons)

    if candidate.requested_mode == "formal":
        if reasons:
            return _rejected("formal", reasons)
        return GovernanceDecision(
            allowed=True,
            mode="formal",
            requested_mode="formal",
            reasons=(),
            assessment_eligible=True,
        )

    if not candidate.explicit_experimental_consent:
        return _rejected(
            "experimental",
            [
                *reasons,
                GovernanceReason(
                    code="EXPERIMENTAL_CONSENT_REQUIRED",
                    message="实验性发布必须由用户显式确认。",
                ),
            ],
        )
    blocking_gap_ids = {
        gap.id
        for gap in candidate.knowledge_gaps
        if gap.severity == "blocking"
        and gap.status not in CLOSED_GAP_STATUSES
        and not gap.experimental_allowed
    }
    if blocking_gap_ids:
        return _rejected(
            "experimental",
            [
                *reasons,
                GovernanceReason(
                    code="KNOWLEDGE_GAP_NOT_EXPERIMENTALLY_OVERRIDABLE",
                    message="该知识缺口不可通过实验性模式覆盖。",
                    subject_ids=tuple(sorted(blocking_gap_ids)),
                ),
            ],
        )
    warnings = tuple(
        GovernanceReason(
            code=reason.code,
            message=reason.message,
            severity="warning",
            subject_ids=reason.subject_ids,
        )
        for reason in reasons
    )
    return GovernanceDecision(
        allowed=True,
        mode="experimental",
        requested_mode="experimental",
        reasons=warnings,
        assessment_eligible=False,
    )

def evaluate_quiz_publication(candidate: QuizGovernanceInput) -> GovernanceDecision:
    content_decision = evaluate_content_publication(candidate.content)
    if not content_decision.allowed:
        return content_decision

    blocks_by_id, claims_by_id, _, bindings_by_claim = _indexes(candidate.content)
    taught_targets = {
        target_id
        for block in candidate.content.blocks
        for target_id in block.assessment_target_ids
    }
    question_reasons: list[GovernanceReason] = []
    duplicate_question_ids = _duplicates(question.id for question in candidate.questions)
    if duplicate_question_ids:
        question_reasons.append(
            GovernanceReason(
                code="DUPLICATE_QUESTION_ID",
                message="题目 ID 必须唯一。",
                subject_ids=tuple(sorted(duplicate_question_ids)),
            )
        )

    for question in candidate.questions:
        target_id = question.primary_assessment_target_id
        if target_id not in candidate.contract_assessment_target_ids:
            question_reasons.append(
                GovernanceReason(
                    code="QUESTION_TARGET_NOT_IN_CONTRACT",
                    message="题目主要测量目标不属于当前学习契约。",
                    subject_ids=(question.id, target_id),
                )
            )
        elif target_id not in taught_targets:
            question_reasons.append(
                GovernanceReason(
                    code="QUESTION_TARGET_NOT_TAUGHT",
                    message="题目主要测量目标未在当前正文中教学。",
                    subject_ids=(question.id, target_id),
                )
            )

        if not question.claim_ids:
            question_reasons.append(
                GovernanceReason(
                    code="QUESTION_CLAIM_REQUIRED",
                    message="题目必须声明作答所依赖的原子主张。",
                    subject_ids=(question.id,),
                )
            )
            continue
        for claim_id in question.claim_ids:
            claim = claims_by_id.get(claim_id)
            if not claim or claim.block_id not in blocks_by_id:
                question_reasons.append(
                    GovernanceReason(
                        code="QUESTION_CLAIM_NOT_TAUGHT",
                        message="题目依赖的主张未锚定到当前正文。",
                        subject_ids=(question.id, claim_id),
                    )
                )
                continue
            claim_block = blocks_by_id[claim.block_id]
            if target_id not in claim_block.assessment_target_ids:
                question_reasons.append(
                    GovernanceReason(
                        code="QUESTION_CLAIM_NOT_TAUGHT_FOR_TARGET",
                        message="题目依赖的主张未作为该测量目标的教学依据。",
                        subject_ids=(question.id, claim_id, target_id),
                    )
                )
            if not _claim_has_verified_support(claim_id, bindings_by_claim):
                question_reasons.append(
                    GovernanceReason(
                        code="QUESTION_CLAIM_UNSUPPORTED",
                        message="题目依赖的主张缺少已核验且可定位的来源支持。",
                        subject_ids=(question.id, claim_id),
                    )
                )

    if question_reasons:
        return _rejected(
            candidate.content.requested_mode,
            [*content_decision.reasons, *question_reasons],
        )
    return GovernanceDecision(
        allowed=True,
        mode=content_decision.mode,
        requested_mode=content_decision.requested_mode,
        reasons=content_decision.reasons,
        assessment_eligible=content_decision.assessment_eligible,
    )


def _duplicates(values) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _rejected(
    requested_mode: PublicationMode,
    reasons: list[GovernanceReason] | tuple[GovernanceReason, ...],
) -> GovernanceDecision:
    return GovernanceDecision(
        allowed=False,
        mode="rejected",
        requested_mode=requested_mode,
        reasons=tuple(reasons),
        assessment_eligible=False,
    )
