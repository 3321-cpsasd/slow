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
