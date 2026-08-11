import asyncio

from app.evaluation.synthetic_study import (
    SyntheticAblationAdapter,
    forced_failure_answers,
)


class CapturingDelegate:
    async def lesson_content(self, request, memory, prior_questions=None):
        return {"request": request, "memory": memory}


def test_no_memory_adapter_removes_only_longitudinal_memory():
    request = {
        "generationContext": {
            "learner": {"planRole": "student"},
            "learningState": {
                "relevantMemory": [
                    {"assessmentTargetId": "target_1", "mastery": 80}
                ],
                "attempt": {"attemptId": "attempt_1", "passed": False},
            },
            "learningContract": {"id": "contract_1"},
        }
    }
    adapter = SyntheticAblationAdapter(CapturingDelegate(), "NO_MEMORY")

    delivered = asyncio.run(
        adapter.lesson_content(
            request,
            [{"assessmentTargetId": "target_1", "mastery": 80}],
        )
    )

    state = delivered["request"]["generationContext"]["learningState"]
    assert state["relevantMemory"] == []
    assert state["attempt"] == {"attemptId": "attempt_1", "passed": False}
    assert delivered["request"]["generationContext"]["learner"] == {
        "planRole": "student"
    }
    assert delivered["request"]["generationContext"]["learningContract"] == {
        "id": "contract_1"
    }
    assert delivered["memory"] == []
    assert adapter.context_audit[0]["preMemoryCount"] == 1
    assert adapter.context_audit[0]["deliveredMemoryCount"] == 0
    assert adapter.context_audit[0]["removedEvidenceIds"] == ["target_1"]


def test_full_adapter_preserves_canonical_provider_payload():
    request = {
        "generationContext": {
            "learningState": {
                "relevantMemory": [{"assessmentTargetId": "target_1"}],
                "attempt": {"attemptId": "attempt_1"},
            }
        }
    }
    memory = [{"assessmentTargetId": "target_1"}]
    adapter = SyntheticAblationAdapter(CapturingDelegate(), "FULL")

    delivered = asyncio.run(adapter.lesson_content(request, memory))

    assert delivered == {"request": request, "memory": memory}
    audit = adapter.context_audit[0]
    assert audit["preTransformHash"] == audit["deliveredHash"]
    assert audit["preMemoryCount"] == audit["deliveredMemoryCount"] == 1
    assert audit["removedEvidenceIds"] == []


def test_forced_failure_is_below_eighty_percent_and_uses_primary_distractor():
    questions = [
        {"options": ["a", "b", "c", "d"], "correct": [index % 4]}
        for index in range(5)
    ]

    answers, wrong_indexes = forced_failure_answers(
        questions,
        primary_question_index=1,
        primary_distractor_indexes=[3],
    )

    correct_count = sum(
        set(answer) == set(question["correct"])
        for answer, question in zip(answers, questions, strict=True)
    )
    assert correct_count / len(questions) < 0.8
    assert wrong_indexes == [1, 0]
    assert answers[1] == [3]
