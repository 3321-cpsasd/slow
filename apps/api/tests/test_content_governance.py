import pytest

from app.modules.learning.content_governance import (
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


def _blocks() -> tuple[ContentBlockInput, ...]:
    return (
        ContentBlockInput(
            "conclusion",
            "conclusion",
            assessment_target_ids=("target_core",),
            assessment_eligible=True,
        ),
        ContentBlockInput("mechanism", "mechanism"),
        ContentBlockInput("example", "example"),
        ContentBlockInput(
            "boundary",
            "boundary",
            assessment_target_ids=("target_core",),
        ),
        ContentBlockInput("practice", "practice"),
        ContentBlockInput("transition", "transition"),
    )


def _claims() -> tuple[SourceClaimInput, ...]:
    return (
        SourceClaimInput(
            "claim_core",
            "conclusion",
            kind="core_conclusion",
            explicitly_assessable=True,
        ),
        SourceClaimInput("claim_boundary", "boundary", kind="boundary"),
    )


def _bindings() -> tuple[SourceClaimBindingInput, ...]:
    return (
        SourceClaimBindingInput(
            "claim_core",
            "source_v1",
            "supports",
            "verified",
            locator="section 2.1",
        ),
        SourceClaimBindingInput(
            "claim_boundary",
            "source_v1",
            "supports",
            "cross_source",
            locator="page 18",
        ),
    )


def _content(**updates) -> ContentGovernanceInput:
    values = {
        "blocks": _blocks(),
        "claims": _claims(),
        "claim_bindings": _bindings(),
    }
    values.update(updates)
    return ContentGovernanceInput(**values)


def _reason_codes(decision) -> set[str]:
    return {reason.code for reason in decision.reasons}


def test_formal_content_requires_evidence_integrity_not_a_fixed_role_template() -> None:
    decision = evaluate_content_publication(_content())
    assert decision.allowed is True
    assert decision.mode == "formal"
    assert decision.assessment_eligible is True
    assert decision.reasons == ()
    assert decision.as_dict()["ruleVersion"] == CONTENT_GOVERNANCE_RULE_VERSION


def test_explicit_assessable_fact_is_strict_even_in_mechanism_block() -> None:
    candidate = _content(
        claims=(
            *_claims(),
            SourceClaimInput(
                "claim_mechanism_fact",
                "mechanism",
                explicitly_assessable=True,
            ),
        )
    )
    decision = evaluate_content_publication(candidate)
    assert decision.allowed is False
    assert "STRICT_CLAIM_UNSUPPORTED" in _reason_codes(decision)
    assert "claim_mechanism_fact" in next(
        reason.subject_ids
        for reason in decision.reasons
        if reason.code == "STRICT_CLAIM_UNSUPPORTED"
    )


def test_blocking_gap_requires_explicit_experimental_mode() -> None:
    gap = KnowledgeGapInput(
        id="gap_missing_source",
        gap_type="missing_source",
        severity="blocking",
    )
    formal = evaluate_content_publication(_content(knowledge_gaps=(gap,)))
    implicit = evaluate_content_publication(
        _content(knowledge_gaps=(gap,), requested_mode="experimental")
    )
    experimental = evaluate_content_publication(
        _content(
            knowledge_gaps=(gap,),
            requested_mode="experimental",
            explicit_experimental_consent=True,
        )
    )
    assert formal.allowed is False
    assert formal.mode == "rejected"
    assert implicit.allowed is False
    assert "EXPERIMENTAL_CONSENT_REQUIRED" in _reason_codes(implicit)
    assert experimental.allowed is True
    assert experimental.mode == "experimental"
    assert experimental.assessment_eligible is False
    assert all(reason.severity == "warning" for reason in experimental.reasons)


def test_experimental_mode_does_not_require_fixed_roles_and_cannot_override_hard_gap() -> None:
    incomplete = evaluate_content_publication(
        _content(
            blocks=tuple(block for block in _blocks() if block.role != "practice"),
            requested_mode="experimental",
            explicit_experimental_consent=True,
        )
    )
    hard_gap = KnowledgeGapInput(
        id="gap_out_of_scope",
        gap_type="out_of_scope",
        severity="blocking",
        experimental_allowed=False,
    )
    blocked = evaluate_content_publication(
        _content(
            knowledge_gaps=(hard_gap,),
            requested_mode="experimental",
            explicit_experimental_consent=True,
        )
    )
    assert incomplete.allowed is True
    assert blocked.allowed is False
    assert "KNOWLEDGE_GAP_NOT_EXPERIMENTALLY_OVERRIDABLE" in _reason_codes(
        blocked
    )


def test_empirical_case_requires_a_traceable_claim_independent_of_role_name() -> None:
    candidate = _content(
        blocks=(
            *_blocks(),
            ContentBlockInput(
                "case_1",
                "empirical_case",
                case_kind="empirical_case",
            ),
        )
    )
    decision = evaluate_content_publication(candidate)
    assert decision.allowed is False
    assert "STRICT_CLAIM_MISSING" in _reason_codes(decision)


def test_quiz_requires_contract_target_taught_target_and_supported_claim() -> None:
    valid = evaluate_quiz_publication(
        QuizGovernanceInput(
            content=_content(),
            questions=(
                QuestionDependencyInput(
                    "question_1",
                    "target_core",
                    ("claim_core",),
                ),
            ),
            contract_assessment_target_ids=frozenset({"target_core"}),
        )
    )
    assert valid.allowed is True
    assert valid.assessment_eligible is True

    not_in_contract = evaluate_quiz_publication(
        QuizGovernanceInput(
            content=_content(),
            questions=(
                QuestionDependencyInput(
                    "question_1",
                    "target_other",
                    ("claim_core",),
                ),
            ),
            contract_assessment_target_ids=frozenset({"target_core"}),
        )
    )
    assert not_in_contract.allowed is False
    assert "QUESTION_TARGET_NOT_IN_CONTRACT" in _reason_codes(not_in_contract)


@pytest.mark.parametrize(
    ("question", "expected_code"),
    [
        (
            QuestionDependencyInput(
                "question_untaught_target",
                "target_untaught",
                ("claim_core",),
            ),
            "QUESTION_TARGET_NOT_TAUGHT",
        ),
        (
            QuestionDependencyInput(
                "question_missing_claim",
                "target_core",
                ("claim_missing",),
            ),
            "QUESTION_CLAIM_NOT_TAUGHT",
        ),
        (
            QuestionDependencyInput(
                "question_no_claim",
                "target_core",
                (),
            ),
            "QUESTION_CLAIM_REQUIRED",
        ),
    ],
)
def test_quiz_rejects_untaught_dependencies(question, expected_code) -> None:
    decision = evaluate_quiz_publication(
        QuizGovernanceInput(
            content=_content(),
            questions=(question,),
            contract_assessment_target_ids=frozenset(
                {"target_core", "target_untaught"}
            ),
        )
    )
    assert decision.allowed is False
    assert expected_code in _reason_codes(decision)


def test_quiz_never_uses_unsupported_claim_even_in_experimental_mode() -> None:
    candidate = _content(
        claim_bindings=tuple(
            binding
            for binding in _bindings()
            if binding.claim_id != "claim_core"
        ),
        requested_mode="experimental",
        explicit_experimental_consent=True,
    )
    content_decision = evaluate_content_publication(candidate)
    quiz_decision = evaluate_quiz_publication(
        QuizGovernanceInput(
            content=candidate,
            questions=(
                QuestionDependencyInput(
                    "question_1",
                    "target_core",
                    ("claim_core",),
                ),
            ),
            contract_assessment_target_ids=frozenset({"target_core"}),
        )
    )
    assert content_decision.allowed is True
    assert content_decision.mode == "experimental"
    assert quiz_decision.allowed is False
    assert "QUESTION_CLAIM_UNSUPPORTED" in _reason_codes(quiz_decision)
