from app.ai.contracts import GeneratedLessonSlotCandidate
from app.application.lesson_composition import resolve_lesson_composition_policy
from app.application.lesson_generation import LessonCompositionPolicy
from app.application.lesson_generation import CandidateValidationFailure, validate_lesson_candidate
from test_lesson_generation_v2 import candidate, spec
import pytest


def _target(objective: str) -> dict:
    return {
        "assessmentTargetId": "target_1",
        "objective": objective,
        "dimension": "recognition",
        "verificationPolicy": "choice_quiz_v1",
    }


def test_social_explanation_requests_distinct_cases_without_changing_targets():
    policy = resolve_lesson_composition_policy(
        section={"title": "制度与群体行为", "question": "社会制度为什么改变群体选择？"},
        targets=[_target("解释制度影响行为的机制")],
    )
    validated = LessonCompositionPolicy.model_validate(policy)
    assert validated.profile == "social_empirical"
    assert validated.case_policy.minimum_distinct_cases == 2
    assert "empirical_case" in validated.recommended_roles


def test_textual_interpretation_prefers_evidence_and_alternative_readings():
    policy = resolve_lesson_composition_policy(
        section={"title": "文本细读", "question": "这段修辞如何支持作品主题？"},
        targets=[_target("依据原文解释修辞作用")],
    )
    assert policy["profile"] == "textual_argumentative"
    assert "primary_source" in policy["recommendedRoles"]
    assert "alternative_interpretation" in policy["recommendedRoles"]


def test_generic_policy_is_explicitly_auditable_and_advisory():
    policy = resolve_lesson_composition_policy(
        section={"title": "一个新概念", "question": "它解决了什么问题？"},
        targets=[_target("解释概念的作用")],
    )
    assert policy["profile"] == "generic_conceptual"
    assert policy["basis"] == "frozen_contract_deterministic_inference"
    assert policy["matchedSignals"] == []


def test_weighted_signals_prefer_derivation_over_general_economic_context():
    policy = resolve_lesson_composition_policy(
        section={"title": "经济模型", "question": "如何推导经济模型的方程？"},
        targets=[_target("推导经济模型的方程")],
    )
    assert policy["profile"] == "formal_quantitative"


def test_english_signals_use_word_boundaries():
    policy = resolve_lesson_composition_policy(
        section={"title": "Proof review", "question": "Find the flaw in this proof"},
        targets=[_target("Evaluate the proof")],
    )
    assert policy["profile"] == "formal_quantitative"
    assert "law" not in policy["matchedSignals"]


def test_dynamic_candidate_no_longer_requires_fixed_shared_slots():
    candidate = GeneratedLessonSlotCandidate.model_validate(
        {
            "blocks": [
                {
                    "slot": "T1_CORE",
                    "kind": "text",
                    "primary_role": "core_instruction",
                    "heading": "直接依据",
                    "content": "这一段直接回答冻结目标，并给出足以支持后续选择题判断的完整依据，同时说明判断成立所需要的条件和范围。",
                },
                {
                    "slot": "S1",
                    "kind": "text",
                    "primary_role": "alternative_interpretation",
                    "teaching_moves": ["compare_interpretations"],
                    "heading": "另一种解释",
                    "content": "这一段提供另一条解释路径，帮助学习者比较前提、证据范围与结论差异，同时避免把一种解释误当成唯一可能。",
                },
            ],
            "questions": [
                {
                    "target_slot": "T1",
                    "prompt": "哪一项符合正文依据？",
                    "options": ["第一项", "第二项", "第三项"],
                    "correct": [1],
                    "explanation": "“第二项”与核心依据一致。",
                }
                for _ in range(4)
            ],
        }
    )
    assert [block.slot for block in candidate.blocks] == ["T1_CORE", "S1"]


def test_empirical_case_role_cannot_hide_an_untyped_or_unsourced_case():
    value = candidate()
    value.blocks[2].role = "empirical_case"
    value.blocks[2].relation_to_anchor = "evidence"
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)
    assert raised.value.code == "CONTENT_CASE_KIND_INVALID"


def test_composition_minimum_blocks_is_a_publication_gate():
    lesson_spec = spec()
    lesson_spec.composition_policy.minimum_blocks = len(candidate().blocks) + 1
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(lesson_spec, candidate())
    assert raised.value.code == "CONTENT_COMPOSITION_MINIMUM_BLOCKS"


def test_composition_minimum_cases_is_a_publication_gate():
    value = candidate()
    lesson_spec = spec()
    lesson_spec.composition_policy.case_policy.minimum_distinct_cases = sum(
        1 for block in value.blocks if block.case_kind
    ) + 1
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(lesson_spec, value)
    assert raised.value.code == "CONTENT_COMPOSITION_CASES_MISSING"


def test_composition_counts_unique_case_keys_not_case_blocks():
    value = candidate()
    value.blocks[2].case_kind = "hypothetical_example"
    value.blocks[2].case_key = "shared_case"
    value.blocks[3].case_kind = "hypothetical_example"
    value.blocks[3].case_key = "shared_case"
    lesson_spec = spec()
    lesson_spec.composition_policy.case_policy.minimum_distinct_cases = 2
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(lesson_spec, value)
    assert raised.value.code == "CONTENT_COMPOSITION_CASES_MISSING"


def test_case_identity_allows_provenance_and_teaching_use_to_vary_by_block():
    value = candidate()
    value.blocks[2].case_kind = "worked_example"
    value.blocks[2].case_key = "shared_case"
    value.blocks[3].case_kind = "hypothetical_example"
    value.blocks[3].case_key = "shared_case"

    validated = validate_lesson_candidate(spec(), value)

    assert validated.candidate is value


def test_case_identity_rejects_factual_and_hypothetical_provenance_conflict():
    value = candidate()
    value.blocks[2].case_kind = "empirical_case"
    value.blocks[2].case_key = "shared_case"
    value.blocks[2].claim_version_ids = ["claim_1"]
    value.blocks[3].case_kind = "hypothetical_example"
    value.blocks[3].case_key = "shared_case"

    lesson_spec = spec()
    lesson_spec.knowledge_context = {
        "status": "ready",
        "claims": [{
            "claimVersionId": "claim_1",
            "scope": {"conceptRevisionIds": []},
        }],
    }
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(lesson_spec, value)

    assert raised.value.code == "CONTENT_CASE_PROVENANCE_CONFLICT"
    assert raised.value.location == {
        "caseKey": "shared_case",
        "expectedCaseProvenance": "factual",
        "actualCaseProvenance": "hypothetical",
    }
