import hashlib
import json

import pytest
from pydantic import ValidationError

from app.evaluation.m3_acceptance import (
    HARD_ZERO_KEYS,
    M3Report,
    evaluate_report,
)


FORMS = [
    "formal_quantitative",
    "technical_procedural",
    "scientific_causal",
    "textual_historical",
    "social_normative",
]
TASKS = ["concept", "mechanism", "boundary", "transfer"]


def report_payload(*, status="pass", diagnosis_macro=0.80):
    samples = [
        {
            "id": f"{form}-{task}",
            "knowledge_form": form,
            "task": task,
            "title": f"{form} {task}",
        }
        for form in FORMS
        for task in TASKS
    ]
    manifest_hash = hashlib.sha256(
        json.dumps(samples, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    def evidence(*modes):
        return [
            {
                "id": f"evidence-{mode}",
                "run_mode": mode,
                "uri": f"artifact://m3/{mode}",
                "content_hash": "a" * 64,
            }
            for mode in modes
        ]
    return {
        "schema_version": "m3_acceptance_v2",
        "sample_freeze": {
            "frozen_at": "2026-08-12T00:00:00+08:00",
            "samples": samples,
            "manifest_hash": manifest_hash,
        },
        "hard_zero": {key: 0 for key in HARD_ZERO_KEYS},
        "gates": [
            {"id": "M3-A", "status": status, "evidence": evidence("fault_drill"), "metrics": {"modelFailureDisclosureRate": 1.0, "modelRecoveryPassRate": 1.0}},
            {"id": "M3-B", "status": status, "evidence": evidence("human_review"), "metrics": {"diagnosisMacroAccuracy": diagnosis_macro, "diagnosisMinimumClassAccuracy": 0.70, "abstentionAccuracy": 0.90}},
            {"id": "M3-C", "status": status, "evidence": evidence("pilot"), "metrics": {"remediationPublishRate": 0.90, "targetRecoveryRate": 0.70}},
            {"id": "M3-D", "status": status, "evidence": evidence("real_model", "human_review"), "metrics": {"lessonPublishRate": 0.90, "qualityAverage": 80, "qualityMinimumKnowledgeForm": 75}},
            {"id": "M3-E", "status": status, "evidence": evidence("pilot"), "metrics": {"confirmedPreferenceCompliance": 0.85}},
        ],
    }


def test_exact_thresholds_pass_when_all_evidence_is_present():
    result = evaluate_report(M3Report.model_validate(report_payload()))
    assert result["result"] == "PASS"
    assert result["sampleCount"] == 20


def test_not_run_or_below_threshold_fails_closed():
    result = evaluate_report(M3Report.model_validate(
        report_payload(status="not_run", diagnosis_macro=0.79)
    ))
    assert result["result"] == "FAIL"
    assert "M3-B:diagnosisMacroAccuracy" in result["failures"]


def test_missing_gate_or_unknown_hard_zero_is_rejected():
    payload = report_payload()
    payload["gates"].pop()
    payload["hard_zero"]["unknown"] = 0
    with pytest.raises(ValidationError):
        M3Report.model_validate(payload)


def test_wrong_evidence_mode_cannot_masquerade_as_pilot_or_human_review():
    payload = report_payload()
    for gate in payload["gates"]:
        gate["evidence"] = [{
            "id": "fixture-only",
            "run_mode": "deterministic_fixture",
            "uri": "artifact://m3/fixture-only",
            "content_hash": "b" * 64,
        }]

    result = evaluate_report(M3Report.model_validate(payload))

    assert result["result"] == "FAIL"
    assert "M3-A:missing_fault_drill_evidence" in result["failures"]
    assert "M3-C:missing_pilot_evidence" in result["failures"]
    assert "M3-D:missing_real_model_evidence" in result["failures"]
