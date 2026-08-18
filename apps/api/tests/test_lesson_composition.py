from app.ai.contracts import GeneratedLessonSlotCandidate
from app.ai.openai_adapter import _expand_lesson_slots
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


def test_social_explanation_treats_case_forms_as_advisory_without_claims():
    policy = resolve_lesson_composition_policy(
        section={"title": "制度与群体行为", "question": "社会制度为什么改变群体选择？"},
        targets=[_target("解释制度影响行为的机制")],
    )
    validated = LessonCompositionPolicy.model_validate(policy)
    assert validated.profile == "social_empirical"
    assert validated.case_policy.minimum_distinct_cases == 0
    assert "empirical_case" not in validated.recommended_roles
    assert "empirical_case" not in validated.case_policy.preferred_kinds


def test_social_explanation_may_prefer_empirical_cases_when_claims_are_available():
    policy = resolve_lesson_composition_policy(
        section={"title": "制度与群体行为", "question": "社会制度为什么改变群体选择？"},
        targets=[_target("解释制度影响行为的机制")],
        knowledge_context={
            "status": "ready",
            "claims": [{"claimVersionId": "claim_1"}],
        },
    )
    validated = LessonCompositionPolicy.model_validate(policy)
    assert validated.case_policy.minimum_distinct_cases == 0
    assert "empirical_case" in validated.recommended_roles
    assert "empirical_case" in validated.case_policy.preferred_kinds


def test_textual_interpretation_avoids_unsourced_primary_material():
    policy = resolve_lesson_composition_policy(
        section={"title": "文本细读", "question": "这段修辞如何支持作品主题？"},
        targets=[_target("依据原文解释修辞作用")],
    )
    assert policy["profile"] == "textual_argumentative"
    assert "primary_source" not in policy["recommendedRoles"]
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


def test_provider_extras_are_ignored_and_fixed_slot_metadata_is_server_owned():
    value = GeneratedLessonSlotCandidate.model_validate(
        {
            "unknown_candidate_note": "模型自行补充但服务端不采用",
            "blocks": [
                {
                    "slot": "T1_CORE",
                    "kind": "unknown_visual_kind",
                    "primary_role": "not_a_real_role",
                    "teaching_moves": ["not_a_real_move"],
                    "unknown_block_note": "不进入正式内容",
                    "heading": "直接依据",
                    "content": "- 第一条给出目标成立的判断依据和适用范围。\n- 第二条说明如何利用这些依据完成后续判断。",
                },
                {
                    "slot": "S1",
                    "primary_role": "mechanism",
                    "heading": "机制补充",
                    "content": "这一段补充解释判断依据之间的因果关系，并明确哪些条件改变时原结论不再成立，帮助学习者避免机械套用。",
                },
            ],
            "questions": [
                {
                    "target_slot": "T1",
                    "prompt": f"第 {index} 题：哪一项符合正文依据？",
                    "options": ["第一项", "第二项", "第三项"],
                    "correct": [1],
                    "explanation": "第二项符合正文明确给出的判断依据。",
                    "unknown_question_note": "同样不采用",
                }
                for index in range(1, 5)
            ],
        }
    )

    block_dump = value.blocks[0].model_dump()
    assert "kind" not in block_dump
    assert "teaching_moves" not in block_dump
    assert "unknown_block_note" not in block_dump
    assert "unknown_candidate_note" not in value.model_dump()
    assert "unknown_question_note" not in value.questions[0].model_dump()

    expanded = _expand_lesson_slots(
        value,
        {
            "learningContractVersionId": "contract_1",
            "targets": [
                {
                    "assessmentTargetId": "target_1",
                    "objective": "根据正文依据完成判断",
                    "required": True,
                }
            ],
            "knowledgeContext": {"status": "not_applicable", "claims": []},
            "compositionPolicy": {"recommendedRoles": ["mechanism"]},
            "feedback": {},
        },
    )

    assert expanded.blocks[0].kind == "bullet_list"
    assert expanded.blocks[0].role == "core_instruction"
    assert expanded.blocks[0].teaching_moves == ["direct_explanation"]
    assert expanded.blocks[1].role == "mechanism"
    assert expanded.blocks[1].teaching_moves == ["explain_mechanism"]


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


def test_composition_case_count_is_advisory_not_a_publication_gate():
    value = candidate()
    lesson_spec = spec()
    lesson_spec.composition_policy.case_policy.minimum_distinct_cases = sum(
        1 for block in value.blocks if block.case_kind
    ) + 1
    validated = validate_lesson_candidate(lesson_spec, value)
    assert validated.candidate is value


def test_composition_does_not_require_distinct_case_keys_for_advisory_policy():
    value = candidate()
    value.blocks[2].case_kind = "hypothetical_example"
    value.blocks[2].case_key = "shared_case"
    value.blocks[3].case_kind = "hypothetical_example"
    value.blocks[3].case_key = "shared_case"
    lesson_spec = spec()
    lesson_spec.composition_policy.case_policy.minimum_distinct_cases = 2
    validated = validate_lesson_candidate(lesson_spec, value)
    assert validated.candidate is value


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
