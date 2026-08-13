import pytest
from pydantic import ValidationError

from app.evaluation.m4_acceptance import HARD_ZERO_KEYS, M4Report, evaluate_report


def report_payload(*, status="pass", repeat_rate=0.0):
    def evidence(*modes):
        return [
            {
                "id": f"evidence-{mode}",
                "run_mode": mode,
                "uri": f"artifact://m4/{mode}",
                "content_hash": "a" * 64,
            }
            for mode in modes
        ]

    return {
        "schema_version": "m4_acceptance_v1",
        "scenarios": [
            {"id": "trusted_assessment", "title": "可信测评"},
            {"id": "ask_me_role_separation", "title": "口试职责分离"},
            {"id": "cross_book_adaptation", "title": "跨书适应"},
            {"id": "scope_and_fault_closure", "title": "范围与故障关闭"},
        ],
        "hard_zero": {key: 0 for key in HARD_ZERO_KEYS},
        "gates": [
            {
                "id": "M4-A",
                "status": status,
                "evidence": evidence("real_model", "fault_drill"),
                "metrics": {
                    "formalPathCoverage": 1.0,
                    "answerVersionCoverage": 1.0,
                    "modelFamilyIndependenceRate": 1.0,
                    "faultClosureRate": 1.0,
                },
            },
            {
                "id": "M4-B",
                "status": status,
                "evidence": evidence("real_model", "fault_drill"),
                "metrics": {
                    "probeEvaluationSeparationRate": 1.0,
                    "probeLineageCoverage": 1.0,
                    "frozenEvidenceBindingRate": 1.0,
                    "idempotentResumeRate": 1.0,
                },
            },
            {
                "id": "M4-C",
                "status": status,
                "evidence": evidence("real_model", "human_review"),
                "metrics": {
                    "stableConceptReuseRate": 1.0,
                    "teachingActionDecisionCoverage": 1.0,
                    "actionComplianceRate": 0.95,
                    "severeCrossBookRepeatRate": repeat_rate,
                },
            },
            {
                "id": "M4-D",
                "status": status,
                "evidence": evidence("real_model", "fault_drill"),
                "metrics": {
                    "productionOutlineReviewRate": 1.0,
                    "injectedFaultClosureRate": 1.0,
                    "realModelScenarioPassRate": 0.90,
                    "severeAdjacentScopeRepeatRate": repeat_rate,
                },
            },
        ],
    }


def test_complete_m4_report_passes_at_frozen_thresholds():
    assert evaluate_report(M4Report.model_validate(report_payload()))["result"] == "PASS"


def test_not_run_or_any_severe_repeat_fails_closed():
    result = evaluate_report(M4Report.model_validate(
        report_payload(status="not_run", repeat_rate=0.01)
    ))
    assert result["result"] == "FAIL"
    assert "M4-C:severeCrossBookRepeatRate" in result["failures"]
    assert "M4-D:severeAdjacentScopeRepeatRate" in result["failures"]


def test_missing_scenario_or_unknown_hard_zero_is_rejected():
    payload = report_payload()
    payload["scenarios"].pop()
    payload["hard_zero"]["unknown"] = 0
    with pytest.raises(ValidationError):
        M4Report.model_validate(payload)


def test_fixture_only_evidence_cannot_pass_m4():
    payload = report_payload()
    for gate in payload["gates"]:
        gate["evidence"] = [{
            "id": "fixture",
            "run_mode": "deterministic_fixture",
            "uri": "artifact://m4/fixture",
            "content_hash": "b" * 64,
        }]
    result = evaluate_report(M4Report.model_validate(payload))
    assert result["result"] == "FAIL"
    assert "M4-C:missing_human_review_evidence" in result["failures"]
    assert "M4-D:missing_real_model_evidence" in result["failures"]
