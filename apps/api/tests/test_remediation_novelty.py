from app.ai.contracts import ChoiceQuestion
from app.application.section_generation import SectionGenerationCoordinator


def _question(*, prompt="应用公司直接面向谁？", options=None):
    return {
        "prompt": prompt,
        "options": options or ["最终用户", "模型公司", "云服务商"],
        "objective": "区分应用公司与模型公司",
        "difficulty": "standard",
    }


def _novelty_issue(prior, current):
    coordinator = object.__new__(SectionGenerationCoordinator)
    return coordinator._questions_novelty_issue(
        prior,
        current,
        allow_option_reorder_only=True,
    )


def test_remediation_accepts_same_prompt_when_options_are_reordered():
    prior = [_question()]
    reordered = [_question(options=["模型公司", "最终用户", "云服务商"])]

    assert _novelty_issue(prior, reordered) is None


def test_remediation_rejects_an_exact_question_copy():
    prior = [_question()]
    punctuation_only_change = [_question(prompt="应用公司直接面向谁")]

    assert _novelty_issue(prior, punctuation_only_change) == "question_duplicate"


def test_remediation_accepts_a_rephrased_prompt_with_the_same_options():
    prior = [_question()]
    rephrased = [_question(prompt="应用公司的直接服务对象是谁？")]

    assert _novelty_issue(prior, rephrased) is None


def test_exact_remediation_copy_is_reordered_and_correct_answer_is_remapped():
    coordinator = object.__new__(SectionGenerationCoordinator)
    prior = [_question()]
    question = ChoiceQuestion(
        prompt=prior[0]["prompt"],
        options=prior[0]["options"],
        correct=[0],
        core=True,
        objective=prior[0]["objective"],
        explanation="应用公司直接服务最终用户。",
    )

    coordinator._reorder_exact_remediation_duplicates(prior, [question])

    assert question.options == ["模型公司", "云服务商", "最终用户"]
    assert question.correct == [2]
    assert question.options[question.correct[0]] == "最终用户"
    assert _novelty_issue(prior, [question.model_dump()]) is None


def test_full_regeneration_still_rejects_option_reorder_only():
    coordinator = object.__new__(SectionGenerationCoordinator)
    prior = [_question()]
    reordered = [_question(options=["模型公司", "最终用户", "云服务商"])]

    assert coordinator._questions_novelty_issue(prior, reordered) == "prompt_duplicate"
