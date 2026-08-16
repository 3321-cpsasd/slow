import pytest

from app.modules.learning.review_task_plans import ReviewCriterion, plan_review_tasks


def _criteria():
    return (
        ReviewCriterion("bronze", "bronze", 1, "choice_quiz", "choice_quiz_v1"),
        ReviewCriterion("silver_mechanism", "silver", 1, "oral_explanation", "oral_explanation_v1"),
        ReviewCriterion("silver_boundary", "silver", 2, "oral_boundary", "oral_boundary_v1"),
        ReviewCriterion("gold", "gold", 1, "standard_application", "standard_application_v1"),
        ReviewCriterion("diamond", "diamond", 1, "transfer_task", "transfer_task_v1"),
    )


def test_silver_reactivation_is_oral_and_gold_is_only_strengthening():
    plan = plan_review_tasks(
        current_stage="silver",
        criteria=_criteria(),
        missing_criterion_ids=("gold", "diamond"),
        available_criterion_ids=frozenset({
            "bronze", "silver_mechanism", "silver_boundary", "gold"
        }),
        remediation_due=False,
    )

    assert plan["reactivation"] == {
        "purpose": "retention_reactivation",
        "taskKind": "oral_reactivation",
        "stage": "silver",
        "criterionIds": ["silver_mechanism", "silver_boundary"],
        "verificationProtocols": ["oral_explanation_v1", "oral_boundary_v1"],
        "evidenceEffect": "activation_only",
    }
    assert plan["strengthening"]["taskKind"] == "application_strengthening"
    assert plan["strengthening"]["criterionIds"] == ["gold"]


def test_diamond_is_not_recommended_until_route_has_transfer_opportunity():
    plan = plan_review_tasks(
        current_stage="gold",
        criteria=_criteria(),
        missing_criterion_ids=("diamond",),
        available_criterion_ids=frozenset({"bronze", "silver_mechanism", "silver_boundary", "gold"}),
        remediation_due=False,
    )

    assert plan["reactivation"]["taskKind"] == "application_reactivation"
    assert plan["strengthening"] is None


def test_remediation_uses_bronze_diagnostic_without_lowering_current_stage():
    plan = plan_review_tasks(
        current_stage="gold",
        criteria=_criteria(),
        missing_criterion_ids=("diamond",),
        available_criterion_ids=frozenset({"bronze", "silver_mechanism", "silver_boundary", "gold", "diamond"}),
        remediation_due=True,
    )

    assert plan["reactivation"]["taskKind"] == "choice_reactivation"
    assert plan["reactivation"]["stage"] == "bronze"
    assert plan["reactivation"]["evidenceEffect"] == "activation_only"
    assert plan["strengthening"]["taskKind"] == "transfer_strengthening"


def test_plan_fails_closed_when_current_stage_has_no_formal_task():
    with pytest.raises(ValueError, match="no formally available"):
        plan_review_tasks(
            current_stage="gold",
            criteria=_criteria(),
            missing_criterion_ids=("diamond",),
            available_criterion_ids=frozenset({"bronze", "silver_mechanism", "silver_boundary"}),
            remediation_due=False,
        )
