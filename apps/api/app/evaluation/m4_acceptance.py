"""Fail-closed M4 acceptance contract for trustworthy adaptive learning."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXPECTED_GATES = {"M4-A", "M4-B", "M4-C", "M4-D"}
EXPECTED_SCENARIOS = {
    "trusted_assessment",
    "ask_me_role_separation",
    "cross_book_adaptation",
    "scope_and_fault_closure",
}
REQUIRED_EVIDENCE_MODES = {
    "M4-A": {"real_model", "fault_drill"},
    "M4-B": {"real_model", "fault_drill"},
    "M4-C": {"real_model", "human_review"},
    "M4-D": {"real_model", "fault_drill"},
}
HARD_ZERO_KEYS = {
    "answerDeclaredByItemAuthor",
    "answerLeakedByReviewer",
    "sameFamilyAssessmentDecisions",
    "unversionedFormalAnswers",
    "askMeEvaluatorGeneratedPrompts",
    "askMeEvidenceWithoutFrozenBindings",
    "crossBookStableIdentityForks",
    "unreviewedProductionOutlines",
    "partialPublications",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EvidenceRef(StrictModel):
    id: str = Field(min_length=1)
    run_mode: Literal[
        "deterministic_fixture", "real_model", "human_review", "fault_drill"
    ] = Field(alias="runMode")
    uri: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$", alias="contentHash")


class Scenario(StrictModel):
    id: Literal[
        "trusted_assessment",
        "ask_me_role_separation",
        "cross_book_adaptation",
        "scope_and_fault_closure",
    ]
    title: str = Field(min_length=1)


class GateResult(StrictModel):
    id: Literal["M4-A", "M4-B", "M4-C", "M4-D"]
    status: Literal["pass", "fail", "not_run"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    metrics: dict[str, float | int]

    @model_validator(mode="after")
    def passing_gate_has_evidence(self):
        if self.status == "pass" and not self.evidence:
            raise ValueError("passing M4 gates require evidence")
        return self


class M4Report(StrictModel):
    schema_version: Literal["m4_acceptance_v1"] = Field(alias="schemaVersion")
    scenarios: list[Scenario] = Field(min_length=4, max_length=4)
    hard_zero: dict[str, int] = Field(alias="hardZero")
    gates: list[GateResult] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def exact_contract_sets(self):
        gate_ids = [item.id for item in self.gates]
        if len(gate_ids) != len(set(gate_ids)) or set(gate_ids) != EXPECTED_GATES:
            raise ValueError("report must contain every M4 gate exactly once")
        scenario_ids = [item.id for item in self.scenarios]
        if (
            len(scenario_ids) != len(set(scenario_ids))
            or set(scenario_ids) != EXPECTED_SCENARIOS
        ):
            raise ValueError("report must contain every M4 scenario exactly once")
        if set(self.hard_zero) != HARD_ZERO_KEYS:
            raise ValueError("M4 hard-zero checks are incomplete or unknown")
        if any(value < 0 for value in self.hard_zero.values()):
            raise ValueError("M4 hard-zero counters cannot be negative")
        return self


def evaluate_report(report: M4Report) -> dict:
    failures: list[str] = []
    if any(report.hard_zero.values()):
        failures.append("hard_zero_violation")
    gates = {item.id: item for item in report.gates}
    for gate in report.gates:
        if gate.status != "pass":
            failures.append(f"{gate.id}:{gate.status}")
        modes = {item.run_mode for item in gate.evidence}
        for mode in sorted(REQUIRED_EVIDENCE_MODES[gate.id] - modes):
            failures.append(f"{gate.id}:missing_{mode}_evidence")

    minimums = {
        "M4-A": {
            "formalPathCoverage": 1.0,
            "answerVersionCoverage": 1.0,
            "modelFamilyIndependenceRate": 1.0,
            "faultClosureRate": 1.0,
        },
        "M4-B": {
            "probeEvaluationSeparationRate": 1.0,
            "probeLineageCoverage": 1.0,
            "frozenEvidenceBindingRate": 1.0,
            "idempotentResumeRate": 1.0,
        },
        "M4-C": {
            "stableConceptReuseRate": 1.0,
            "teachingActionDecisionCoverage": 1.0,
            "actionComplianceRate": 0.95,
        },
        "M4-D": {
            "productionOutlineReviewRate": 1.0,
            "injectedFaultClosureRate": 1.0,
            "realModelScenarioPassRate": 0.90,
        },
    }
    maximums = {
        "M4-C": {"severeCrossBookRepeatRate": 0.0},
        "M4-D": {"severeAdjacentScopeRepeatRate": 0.0},
    }
    for gate_id, requirements in minimums.items():
        metrics = gates[gate_id].metrics
        for key, minimum in requirements.items():
            value = metrics.get(key)
            if value is None or float(value) < minimum:
                failures.append(f"{gate_id}:{key}")
    for gate_id, requirements in maximums.items():
        metrics = gates[gate_id].metrics
        for key, maximum in requirements.items():
            value = metrics.get(key)
            if value is None or float(value) > maximum:
                failures.append(f"{gate_id}:{key}")
    return {
        "milestone": "M4",
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
        "scenarioCount": len(report.scenarios),
    }


def load_and_evaluate(path: str | Path) -> dict:
    return evaluate_report(
        M4Report.model_validate_json(Path(path).read_text(encoding="utf-8"))
    )
