import pytest

from app.ai.contracts import (
    GeneratedLessonSlotCandidate,
    LessonQuestionAdjudicationBatch,
    LessonQuestionAuthorBatch,
    LessonQuestionReviewBatch,
)
from app.ai.openai_adapter import _apply_lesson_question_review, _expand_lesson_slots


def slot_candidate() -> GeneratedLessonSlotCandidate:
    return GeneratedLessonSlotCandidate.model_validate({
        "blocks": [
            {
                "slot": "T1_CORE",
                "kind": "text",
                "primary_role": "core_instruction",
                "heading": "先计算对齐条件",
                "content": "TMA 要求 stride 是 16 字节的整数倍。96 除以 16 等于 6，所以满足要求。",
            },
            {
                "slot": "S1",
                "kind": "text",
                "primary_role": "boundary",
                "heading": "还要检查其他约束",
                "content": "满足 stride 对齐只说明这一项约束通过，仍需继续检查共享内存容量、基地址对齐和描述符生命周期等独立条件。",
            },
        ],
        "questions": [
            {
                "target_slot": "T1",
                "prompt": "stride 为 96 字节时，是否满足 16 字节整数倍约束？",
                "options": [
                    "可以，因为 96 是 16 的整数倍",
                    "不可以，因为 96 不是 16 的整数倍",
                    "无法由 96 与 16 判断",
                ],
            }
            for _ in range(4)
        ],
    })


def adjudication(indeterminate=False) -> LessonQuestionAdjudicationBatch:
    return LessonQuestionAdjudicationBatch.model_validate({
        "questions": [
            {
                "item_slot": f"Q{position}",
                "option_verdicts": [
                    {
                        "option_id": "O1",
                        "decision": "indeterminate" if indeterminate else "satisfies",
                        "evidence_slot": "T1_CORE",
                        "rationale": "正文不足以确定" if indeterminate else "96 除以 16 等于 6",
                        "cause_code": "",
                    },
                    {
                        "option_id": "O2",
                        "decision": "does_not_satisfy",
                        "evidence_slot": "T1_CORE",
                        "rationale": "该算术前提错误",
                        "cause_code": "mechanism_reasoning_break",
                    },
                    {
                        "option_id": "O3",
                        "decision": "does_not_satisfy",
                        "evidence_slot": "T1_CORE",
                        "rationale": "可以直接由除法判断",
                        "cause_code": "application_transfer_failure",
                    },
                ],
            }
            for position in range(1, 5)
        ],
    })


SPEC = {
    "learningContractVersionId": "contract_1",
    "section": {"id": "section_1"},
    "targets": [{
        "assessmentTargetId": "target_1",
        "objective": "判断 TMA stride 的 16 字节整数倍约束",
        "required": True,
    }],
    "feedback": {},
}


def test_blind_verdict_overrides_a_distractor_that_denies_arithmetic():
    candidate = _expand_lesson_slots(slot_candidate(), SPEC, adjudication())

    for question in candidate.questions:
        assert question.answer_authority == "blind_model_adjudication_v1"
        assert [question.options[index] for index in question.correct] == [
            "可以，因为 96 是 16 的整数倍"
        ]
        assert "96 除以 16 等于 6" in question.explanation


def test_indeterminate_answer_fails_closed():
    with pytest.raises(ValueError, match="indeterminate"):
        _expand_lesson_slots(slot_candidate(), SPEC, adjudication(True))


def test_item_author_schema_rejects_answer_fields():
    with pytest.raises(ValueError):
        LessonQuestionAuthorBatch.model_validate({
            "questions": [
                {
                    "target_slot": "T1",
                    "prompt": f"第 {index} 题：哪项成立？",
                    "options": ["一", "二", "三"],
                    "correct": [0],
                }
                for index in range(1, 5)
            ]
        })


def test_reviewer_can_edit_only_the_fields_that_need_correction():
    reviewed = _apply_lesson_question_review(
        slot_candidate(),
        LessonQuestionReviewBatch.model_validate({
            "questions": [
                {
                    "item_slot": "Q1",
                    "decision": "edit",
                    "issues": ["missing_condition"],
                    "edit": {
                        "prompt": "仅检查 stride 整数倍约束时，96 字节是否满足要求？"
                    },
                },
                *[
                    {"item_slot": f"Q{index}", "decision": "accept"}
                    for index in range(2, 5)
                ],
            ]
        }),
    )

    assert reviewed.questions[0].target_slot == "T1"
    assert reviewed.questions[0].prompt.startswith("仅检查")
    assert reviewed.questions[0].options == slot_candidate().questions[0].options


def test_reviewer_edit_cannot_change_target_or_supply_an_answer():
    with pytest.raises(ValueError):
        LessonQuestionReviewBatch.model_validate({
            "questions": [
                {
                    "item_slot": "Q1",
                    "decision": "edit",
                    "issues": ["target_scope_drift"],
                    "edit": {
                        "target_slot": "T2",
                        "correct": [0],
                        "prompt": "修改后的题目？",
                    },
                },
                *[
                    {"item_slot": f"Q{index}", "decision": "accept"}
                    for index in range(2, 5)
                ],
            ]
        })
