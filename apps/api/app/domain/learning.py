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
    results, score, core_ok = [], 0, True
    for question, answer in zip(questions, answers, strict=True):
        expected = sorted(question["correct"])
        actual = sorted(set(answer))
        correct = actual == expected
        score += int(correct)
        if question.get("core", False) and not correct:
            core_ok = False
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
    passed = core_ok and score / len(questions) >= PASS_RATE
    return Grade(score, len(questions), passed, score == len(questions), results)


def next_status(*, passed: bool) -> str:
    return "completed" if passed else "available"
