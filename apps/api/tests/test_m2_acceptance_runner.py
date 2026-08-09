import hashlib
import json
import pytest
from pathlib import Path
from pydantic import ValidationError

from app.evaluation.m2_runner import (
    M2AcceptanceAudit,
    M2AcceptanceRunner,
    M2AuthoritySnapshot,
    M2FailureCloseProbe,
    M2FailureProbeRunner,
    M2JourneyExecution,
    M2RunnerInputs,
    M2RunnerResult,
    M2TargetExecution,
)
from app.infrastructure.database import build_database
from app.infrastructure.tables import Base
from app.modules.knowledge.fact_graph import KnowledgeFactGraphService


API_ROOT = Path(__file__).resolve().parents[1]


def _probe(scenario: str, *, rejected: bool = True, after_delta: int = 0):
    return M2FailureCloseProbe(
        scenario=scenario,
        rejected=rejected,
        errorCode=f"REJECTED_{scenario.upper()}",
        contentCountBefore=10,
        contentCountAfter=10 + after_delta,
        quizCountBefore=10,
        quizCountAfter=10 + after_delta,
        observationCountBefore=30,
        observationCountAfter=30 + after_delta,
        evidence=[f"probe:{scenario}"],
    )


def _target(position: int, *, code_verified: bool = False, code_evidence=None):
    return M2TargetExecution(
        targetKey=("recursion", "graph_search", "dynamic_programming")[position - 1],
        baselineObjectiveKey=(
            "solve_with_enumeration_recursion_and_search"
            if position < 3
            else "model_and_solve_with_dynamic_programming"
        ),
        conceptRevisionId=f"concept-revision-{position}",
        dependencyPosition=position,
        generationRunId=f"generation-{position}",
        contentVersionId=f"content-{position}",
        quizSetId=f"quiz-{position}",
        observationIds=[f"observation-{position}"],
        knowledgeContext={
            "schemaVersion": "knowledge_context_pack_v1",
            "status": "ready",
            "releaseId": "release-1",
            "baselineVersionId": "baseline-1",
            "budget": {"maxNodes": 3, "maxEdges": 2, "maxHops": 1},
            "actual": {"nodeCount": 3, "edgeCount": 2, "claimCount": 5},
            "truncation": {"truncated": False, "reasons": []},
            "contextHash": f"context-{position}",
        },
        knowledgeDimensionCompleted=True,
        codeCapabilityVerified=code_verified,
        codeEvidenceIds=code_evidence or [],
    )


def _execution(mode="fixture", *, probes=None):
    return M2JourneyExecution(
        executionMode=mode,
        provider="openai" if mode == "real_provider" else "fixture",
        model="acceptance-model",
        seriesId="series-1",
        baselineVersionId="baseline-1",
        graphReleaseId="release-1",
        targets=[_target(1), _target(2), _target(3)],
        failureProbes=probes
        or [
            _probe("baseline_out_of_scope"),
            _probe("knowledge_support_missing"),
            _probe("version_mismatch"),
        ],
        projectionRebuildEvidence=["projection-rebuild:passed"],
    )


def _audit(*, errors=None):
    return M2AcceptanceAudit(
        baselineSourceEvidence=["source:official"],
        baselinePublicationEvidence=["baseline:published"],
        codePolicyEvidence=["code-policy:slow_code_task_v1"],
        graphIdentityEvidence=["graph:published"],
        graphClaimEvidence=["claims:verified"],
        contextPackEvidence=["contexts:bounded"],
        generationAuditEvidence=["generation-runs:audited"],
        journeyEvidence=["journey:three-targets"],
        failureCloseEvidence=["failure-probes:closed"],
        errors=errors or {},
    )


def _inputs():
    return M2RunnerInputs(
        run_id="m2-test",
        code_revision="revision-test",
        baseline_package_path=None,
        baseline_review_path=None,
        knowledge_package_path=None,
        knowledge_review_path=None,
        historical_a1_evidence=("historical:A1",),
        historical_a2_evidence=("historical:A2",),
    )


def _authority():
    return M2AuthoritySnapshot(
        baselineVersionId="baseline-1",
        baselineVersion=1,
        baselineContentHash="a" * 64,
        graphReleaseId="release-1",
        graphReleaseVersion=1,
        graphContentHash="b" * 64,
    )


def test_fixture_runner_can_test_orchestration_but_cannot_pass_m2_e1():
    execution = _execution("fixture")
    acceptance = M2AcceptanceRunner._acceptance(_inputs(), execution, _audit())

    assert acceptance.decision == "FAIL"
    assert acceptance.blocking_gate_ids == ["M2-E1"]
    gate = next(item for item in acceptance.gates if item.gate_id == "M2-E1")
    assert gate.status == "not_run"
    assert "real-provider" in gate.findings[0]


def test_runner_orchestrates_deterministic_driver_and_auditor_fixture():
    execution = _execution("fixture")

    class FixtureDriver:
        def execute(self, authority):
            assert authority == _authority()
            return execution

    class FixtureAuditor:
        def audit(self, authority, received_execution):
            assert authority == _authority()
            assert received_execution == execution
            return _audit()

    class FixtureRunner(M2AcceptanceRunner):
        def install_authority(self, inputs):
            return _authority()

    result = FixtureRunner(
        None,
        driver=FixtureDriver(),
        auditor=FixtureAuditor(),
    ).run(_inputs())

    assert result.acceptance.decision == "FAIL"
    assert result.acceptance.blocking_gate_ids == ["M2-E1"]


def test_real_provider_evidence_passes_only_when_all_audits_and_probes_pass():
    execution = _execution("real_provider")
    acceptance = M2AcceptanceRunner._acceptance(_inputs(), execution, _audit())

    assert acceptance.decision == "PASS"
    assert acceptance.blocking_gate_ids == []


def test_failure_probe_detects_authoritative_write_and_fails_e2():
    execution = _execution(
        "real_provider",
        probes=[
            _probe("baseline_out_of_scope"),
            _probe("knowledge_support_missing", after_delta=1),
            _probe("version_mismatch"),
        ],
    )
    acceptance = M2AcceptanceRunner._acceptance(_inputs(), execution, _audit())

    assert acceptance.decision == "FAIL"
    assert acceptance.blocking_gate_ids == ["M2-E2"]


def test_dependency_path_requires_three_distinct_connected_sample_identities():
    targets = [_target(1), _target(2), _target(2)]
    targets[2].dependency_position = 3
    with pytest.raises(ValidationError, match="dependency targets must be distinct"):
        M2JourneyExecution(
            executionMode="fixture",
            provider="fixture",
            model="fixture",
            seriesId="series-1",
            baselineVersionId="baseline-1",
            graphReleaseId="release-1",
            targets=targets,
            failureProbes=[
                _probe("baseline_out_of_scope"),
                _probe("knowledge_support_missing"),
                _probe("version_mismatch"),
            ],
            projectionRebuildEvidence=["projection-rebuild:passed"],
        )


def test_choice_quiz_cannot_claim_code_capability_without_separate_evidence():
    with pytest.raises(ValidationError, match="separate code evidence"):
        _target(1, code_verified=True)


def test_machine_and_markdown_reports_keep_code_evidence_boundary_visible():
    execution = _execution("real_provider")
    acceptance = M2AcceptanceRunner._acceptance(_inputs(), execution, _audit())
    result = M2RunnerResult(
        authority=_authority(),
        execution=execution,
        audit=_audit(),
        acceptance=acceptance,
    )

    assert result.json_payload()["decision"] == "PASS"
    assert "选择题只验证知识维度" in result.markdown()
    assert "不得据此声称程序设计能力" in result.markdown()


def test_authority_install_and_three_failure_probes_run_in_isolated_sqlite(tmp_path):
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        knowledge_package_path = (
            API_ROOT
            / "knowledge_graph_slices"
            / "pku_recursion_search_dp_v1.json"
        )
        package = KnowledgeFactGraphService.read_package(knowledge_package_path)
        release_material = "\x1f".join(
            (
                package.baseline_version_id,
                str(package.version),
            )
        )
        release_id = (
            "knowledge_graph_release_"
            + hashlib.sha256(release_material.encode()).hexdigest()[:32]
        )
        knowledge_review_path = tmp_path / "knowledge-review.json"
        gap_dispositions = []
        for gap in package.declared_gaps:
            gap_material = "\x1f".join((release_id, gap.code, gap.subject_key))
            gap_dispositions.append(
                {
                    "gapId": "knowledge_graph_gap_"
                    + hashlib.sha256(gap_material.encode()).hexdigest()[:32],
                    "disposition": "acknowledged_warning",
                    "rationale": "deterministic fixture acknowledges scoped warning",
                }
            )
        knowledge_review_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "knowledge_graph_review_v1",
                    "releaseId": release_id,
                    "contentHash": package.content_hash(),
                    "decision": "approved",
                    "reviewerId": "deterministic_test_reviewer",
                    "reviewedAt": "2026-08-09T00:00:00Z",
                    "reviewNote": "deterministic fixture approval only",
                    "acceptedSourceKeys": [item.source_key for item in package.sources],
                    "acceptedClaimKeys": [item.key for item in package.claims],
                    "acceptedRelationKeys": [item.key for item in package.relations],
                    "gapDispositions": gap_dispositions,
                }
            ),
            encoding="utf-8",
        )
        inputs = M2RunnerInputs(
            run_id="isolated-preflight",
            code_revision="revision-test",
            baseline_package_path=(
                API_ROOT
                / "curriculum_baselines"
                / "pku_cs_programming_practice_2025_v1.json"
            ),
            baseline_review_path=(
                API_ROOT
                / "curriculum_baselines"
                / "pku_cs_programming_practice_2025_v1_review_20260809.json"
            ),
            knowledge_package_path=knowledge_package_path,
            knowledge_review_path=knowledge_review_path,
            historical_a1_evidence=("historical:A1",),
            historical_a2_evidence=("historical:A2",),
        )
        runner = M2AcceptanceRunner(db, driver=object(), auditor=object())
        authority = runner.install_authority(inputs)
        probes = M2FailureProbeRunner(
            db,
            knowledge_package_path=inputs.knowledge_package_path,
        ).run()

        assert authority.baseline_version_id.startswith("curriculum_baseline_")
        assert authority.graph_release_id.startswith("knowledge_graph_release_")
        assert {item.scenario for item in probes} == {
            "baseline_out_of_scope",
            "knowledge_support_missing",
            "version_mismatch",
        }
        assert all(item.failed_closed for item in probes)
