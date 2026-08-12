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
    evidence = [{
        "id": "evidence-1", "run_mode": "fault_drill", "uri": "artifact://m3/test",
        "content_hash": "a" * 64,
    }]
    return {
        "schema_version": "m3_acceptance_v1",
        "sample_freeze": {
            "frozen_at": "2026-08-12T00:00:00+08:00",
            "samples": samples,
            "manifest_hash": manifest_hash,
        },
        "hard_zero": {key: 0 for key in HARD_ZERO_KEYS},
        "gates": [
            {"id": "M3-A", "status": status, "evidence": evidence, "metrics": {"outageDrillPassRate": 1.0}},
            {"id": "M3-B", "status": status, "evidence": evidence, "metrics": {"diagnosisMacroAccuracy": diagnosis_macro, "diagnosisMinimumClassAccuracy": 0.70, "abstentionAccuracy": 0.90}},
            {"id": "M3-C", "status": status, "evidence": evidence, "metrics": {"remediationPublishRate": 0.90, "targetRecoveryRate": 0.70}},
            {"id": "M3-D", "status": status, "evidence": evidence, "metrics": {"lessonPublishRate": 0.90, "qualityAverage": 80, "qualityMinimumKnowledgeForm": 75}},
            {"id": "M3-E", "status": status, "evidence": evidence, "metrics": {"confirmedPreferenceCompliance": 0.85}},
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
