from dataclasses import dataclass

PASS_RATE = 0.8


@dataclass(frozen=True)
class Grade:
    score: int
    total: int
    passed: bool
    perfect: bool
    results: list[dict]


def grade_choice_quiz(questions: list[dict], answers: list[list[int]]) -> Grade:
    if not questions or len(questions) != len(answers):
        raise ValueError("答案数量与题目不一致")
    results, score = [], 0
    for question, answer in zip(questions, answers, strict=True):
        expected = sorted(question["correct"])
        actual = sorted(set(answer))
        correct = actual == expected
        score += int(correct)
        results.append(
            {
                "correct": correct,
                "explanation": question["explanation"],
                "objective": question["objective"],
                "selectedOptions": actual,
                "correctOptions": expected,
                "missedOptions": [index for index in expected if index not in actual],
                "incorrectOptions": [index for index in actual if index not in expected],
            }
        )
    core_resolved = all(
        result["correct"]
        for question, result in zip(questions, results, strict=True)
        if question.get("core", False)
    )
    passed = score / len(questions) >= PASS_RATE and core_resolved
    return Grade(score, len(questions), passed, score == len(questions), results)


def passing_score(score: int, total: int) -> bool:
    return total > 0 and score / total >= PASS_RATE


def next_status(*, passed: bool) -> str:
    return "completed" if passed else "available"
