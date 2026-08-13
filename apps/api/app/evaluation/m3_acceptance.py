"""Strict M3 acceptance parser and threshold evaluator.

Engineering fixtures may exercise this module, but only evidence explicitly
marked with its real run mode can satisfy a gate. Missing and not-run evidence
always fail closed.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXPECTED_GATES = {"M3-A", "M3-B", "M3-C", "M3-D", "M3-E"}
EXPECTED_KNOWLEDGE_FORMS = {
    "formal_quantitative",
    "technical_procedural",
    "scientific_causal",
    "textual_historical",
    "social_normative",
}
REQUIRED_EVIDENCE_MODES = {
    "M3-A": {"fault_drill"},
    "M3-B": {"human_review"},
    "M3-C": {"pilot"},
    "M3-D": {"real_model", "human_review"},
    "M3-E": {"pilot"},
}
HARD_ZERO_KEYS = {
    "contractOutsideTargets",
    "danglingEvidenceBindings",
    "partialPublications",
    "lockedContentLeaks",
    "remediationDifficultyDrops",
    "preferenceBoundaryViolations",
    "ungroundedRealCases",
    "modelFailuresMisreportedAsSuccess",
    "fallbackContractMismatches",
    "learningGateBypasses",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EvidenceRef(StrictModel):
    id: str = Field(min_length=1)
    run_mode: Literal[
        "deterministic_fixture", "real_model", "human_review", "fault_drill", "pilot"
    ] = Field(alias="runMode")
    uri: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$", alias="contentHash")


class GateResult(StrictModel):
    id: Literal["M3-A", "M3-B", "M3-C", "M3-D", "M3-E"]
    status: Literal["pass", "fail", "not_run"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    metrics: dict[str, float | int]

    @model_validator(mode="after")
    def evidence_is_required_for_pass(self):
        if self.status == "pass" and not self.evidence:
            raise ValueError("passing gates require evidence")
        return self


class FrozenSample(StrictModel):
    id: str = Field(min_length=1)
    knowledge_form: Literal[
        "formal_quantitative",
        "technical_procedural",
        "scientific_causal",
        "textual_historical",
        "social_normative",
    ] = Field(alias="knowledgeForm")
    task: Literal["concept", "mechanism", "boundary", "transfer"]
    title: str = Field(min_length=1)


class SampleFreeze(StrictModel):
    frozen_at: str = Field(min_length=1, alias="frozenAt")
    samples: list[FrozenSample] = Field(min_length=20)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$", alias="manifestHash")

    @model_validator(mode="after")
    def complete_and_immutable(self):
        ids = [item.id for item in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("sample ids must be unique")
        coverage = {(item.knowledge_form, item.task) for item in self.samples}
        expected = {
            (knowledge_form, task)
            for knowledge_form in EXPECTED_KNOWLEDGE_FORMS
            for task in ("concept", "mechanism", "boundary", "transfer")
        }
        if not expected.issubset(coverage):
            raise ValueError("sample freeze must cover all five forms and four tasks")
        payload = [item.model_dump(mode="json") for item in self.samples]
        actual = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if actual != self.manifest_hash:
            raise ValueError("sample manifest hash mismatch")
        return self


class M3Report(StrictModel):
    schema_version: Literal["m3_acceptance_v2"] = Field(alias="schemaVersion")
    sample_freeze: SampleFreeze = Field(alias="sampleFreeze")
    hard_zero: dict[str, int] = Field(alias="hardZero")
    gates: list[GateResult] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def exact_gate_and_zero_sets(self):
        gate_ids = [item.id for item in self.gates]
        if len(gate_ids) != len(set(gate_ids)) or set(gate_ids) != EXPECTED_GATES:
            raise ValueError("report must contain each M3 gate exactly once")
        if set(self.hard_zero) != HARD_ZERO_KEYS:
            raise ValueError("hard-zero set is missing or contains unknown checks")
        if any(value < 0 for value in self.hard_zero.values()):
            raise ValueError("hard-zero counters cannot be negative")
        return self


def evaluate_report(report: M3Report) -> dict:
    failures: list[str] = []
    if any(report.hard_zero.values()):
        failures.append("hard_zero_violation")
    gate_by_id = {item.id: item for item in report.gates}
    for gate in report.gates:
        if gate.status != "pass":
            failures.append(f"{gate.id}:{gate.status}")
        evidence_modes = {item.run_mode for item in gate.evidence}
        missing_modes = REQUIRED_EVIDENCE_MODES[gate.id] - evidence_modes
        for mode in sorted(missing_modes):
            failures.append(f"{gate.id}:missing_{mode}_evidence")

    thresholds = {
        "M3-A": {
            "modelFailureDisclosureRate": 1.0,
            "modelRecoveryPassRate": 1.0,
        },
        "M3-B": {
            "diagnosisMacroAccuracy": 0.80,
            "diagnosisMinimumClassAccuracy": 0.70,
            "abstentionAccuracy": 0.90,
        },
        "M3-C": {
            "remediationPublishRate": 0.90,
            "targetRecoveryRate": 0.70,
        },
        "M3-D": {
            "lessonPublishRate": 0.90,
            "qualityAverage": 80.0,
            "qualityMinimumKnowledgeForm": 75.0,
        },
        "M3-E": {"confirmedPreferenceCompliance": 0.85},
    }
    for gate_id, requirements in thresholds.items():
        metrics = gate_by_id[gate_id].metrics
        for key, minimum in requirements.items():
            value = metrics.get(key)
            if value is None or float(value) < minimum:
                failures.append(f"{gate_id}:{key}")
    return {
        "milestone": "M3",
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
        "sampleCount": len(report.sample_freeze.samples),
        "sampleManifestHash": report.sample_freeze.manifest_hash,
    }


def load_and_evaluate(path: str | Path) -> dict:
    report = M3Report.model_validate_json(Path(path).read_text(encoding="utf-8"))
    return evaluate_report(report)
