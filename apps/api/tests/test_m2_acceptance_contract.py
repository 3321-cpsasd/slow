import pytest
from pydantic import ValidationError

from app.evaluation.m2_acceptance import (
    M2AcceptanceEvidence,
    M2GateResult,
    M2_HARD_GATES,
)


def _gate_results(*, override: dict[str, str] | None = None):
    override = override or {}
    return [
        M2GateResult(
            gateId=gate_id,
            status=override.get(gate_id, "pass"),
            evidence=[f"evidence:{gate_id}"],
        )
        for gate_id in M2_HARD_GATES
    ]


def test_m2_pass_requires_every_hard_gate_to_pass():
    report = M2AcceptanceEvidence(
        runId="m2-v2-pass",
        codeRevision="revision-1",
        gates=_gate_results(),
    )

    assert report.decision == "PASS"
    assert report.blocking_gate_ids == []


@pytest.mark.parametrize("status", ["fail", "not_run"])
def test_failed_or_missing_execution_cannot_be_hidden_by_findings(status):
    report = M2AcceptanceEvidence(
        runId="m2-v2-fail",
        codeRevision="revision-1",
        gates=_gate_results(override={"M2-D1": status}),
    )
    report.gates[0].findings.append("非阻断观察不改变硬门禁判定")

    assert report.decision == "FAIL"
    assert report.blocking_gate_ids == ["M2-D1"]


def test_report_rejects_missing_gate_or_evidence_free_pass():
    with pytest.raises(ValidationError, match="M2 hard gate mismatch"):
        M2AcceptanceEvidence(
            runId="missing-gate",
            codeRevision="revision-1",
            gates=_gate_results()[:-1],
        )

    results = _gate_results()
    results[0].evidence = []
    with pytest.raises(ValidationError, match="requires evidence"):
        M2AcceptanceEvidence(
            runId="missing-evidence",
            codeRevision="revision-1",
            gates=results,
        )
