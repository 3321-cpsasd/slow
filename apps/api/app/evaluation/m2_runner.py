"""M2 v2 acceptance orchestration and fail-closed evidence validation.

The runner intentionally separates authority installation, the provider journey,
and database audit.  Unit tests can inject deterministic fixtures, while a real
acceptance run must identify itself as ``real_provider`` and provide database
backed artifact references.  Fixture executions can never produce an M2 PASS.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import (
    AssessmentObservation,
    AiInvocation,
    Book,
    Chapter,
    ChapterCurriculumObjectiveBinding,
    ConceptObjectiveBinding,
    Concept,
    ConceptRelationVersion,
    ConceptRevision,
    ContentVersion,
    CourseVersion,
    CurriculumBaselineVersion,
    CurriculumSourceVersion,
    GenerationRun,
    KnowledgeClaimBinding,
    KnowledgeGraphRelease,
    KnowledgeSourceVersion,
    LearningContractConcept,
    LearningContractVersion,
    LearningObjective,
    QuizSet,
    QuizAttempt,
    Section,
    SeriesCurriculumBaselineBinding,
    SourceClaimVersion,
)
from ..modules.curriculum.baselines import CurriculumBaselineService
from ..modules.knowledge.fact_graph import (
    KnowledgeFactGraphService,
    KnowledgeGraphReviewManifest,
)
from .m2_acceptance import M2AcceptanceEvidence, M2GateResult, M2_HARD_GATES


REQUIRED_FAILURE_SCENARIOS = frozenset(
    {
        "baseline_out_of_scope",
        "knowledge_support_missing",
        "version_mismatch",
    }
)


class RunnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class M2AuthoritySnapshot(RunnerModel):
    baseline_version_id: str = Field(alias="baselineVersionId", min_length=1)
    baseline_version: int = Field(alias="baselineVersion", ge=1)
    baseline_content_hash: str = Field(
        alias="baselineContentHash", pattern=r"^[a-f0-9]{64}$"
    )
    graph_release_id: str = Field(alias="graphReleaseId", min_length=1)
    graph_release_version: int = Field(alias="graphReleaseVersion", ge=1)
    graph_content_hash: str = Field(
        alias="graphContentHash", pattern=r"^[a-f0-9]{64}$"
    )


class M2FailureCloseProbe(RunnerModel):
    scenario: Literal[
        "baseline_out_of_scope",
        "knowledge_support_missing",
        "version_mismatch",
    ]
    rejected: bool
    error_code: str = Field(alias="errorCode", min_length=1)
    content_count_before: int = Field(alias="contentCountBefore", ge=0)
    content_count_after: int = Field(alias="contentCountAfter", ge=0)
    quiz_count_before: int = Field(alias="quizCountBefore", ge=0)
    quiz_count_after: int = Field(alias="quizCountAfter", ge=0)
    observation_count_before: int = Field(alias="observationCountBefore", ge=0)
    observation_count_after: int = Field(alias="observationCountAfter", ge=0)
    evidence: list[str] = Field(min_length=1)

    @property
    def failed_closed(self) -> bool:
        return bool(
            self.rejected
            and self.content_count_before == self.content_count_after
            and self.quiz_count_before == self.quiz_count_after
            and self.observation_count_before == self.observation_count_after
        )


class M2TargetExecution(RunnerModel):
    """One knowledge-dimension target in the dependency-path sample."""

    target_key: str = Field(alias="targetKey", min_length=1)
    baseline_objective_key: str = Field(alias="baselineObjectiveKey", min_length=1)
    concept_revision_id: str = Field(alias="conceptRevisionId", min_length=1)
    dependency_position: int = Field(alias="dependencyPosition", ge=1)
    generation_run_id: str = Field(alias="generationRunId", min_length=1)
    content_version_id: str = Field(alias="contentVersionId", min_length=1)
    quiz_set_id: str = Field(alias="quizSetId", min_length=1)
    observation_ids: list[str] = Field(alias="observationIds", min_length=1)
    knowledge_context: dict = Field(alias="knowledgeContext")
    knowledge_dimension_completed: bool = Field(alias="knowledgeDimensionCompleted")
    code_capability_verified: bool = Field(
        default=False,
        alias="codeCapabilityVerified",
        description=(
            "Must stay false unless a separate code/practice artifact was actually scored."
        ),
    )
    code_evidence_ids: list[str] = Field(default_factory=list, alias="codeEvidenceIds")

    @model_validator(mode="after")
    def code_claim_requires_separate_evidence(self):
        if self.code_capability_verified and not self.code_evidence_ids:
            raise ValueError("verified code capability requires separate code evidence")
        return self


class M2JourneyExecution(RunnerModel):
    execution_mode: Literal["fixture", "real_provider"] = Field(alias="executionMode")
    provider: str = ""
    model: str = ""
    series_id: str = Field(alias="seriesId", min_length=1)
    baseline_version_id: str = Field(alias="baselineVersionId", min_length=1)
    graph_release_id: str = Field(alias="graphReleaseId", min_length=1)
    targets: list[M2TargetExecution] = Field(min_length=3)
    failure_probes: list[M2FailureCloseProbe] = Field(
        alias="failureProbes", min_length=3
    )
    projection_rebuild_evidence: list[str] = Field(
        alias="projectionRebuildEvidence", min_length=1
    )

    @model_validator(mode="after")
    def dependency_path_and_probes_are_complete(self):
        target_keys = [item.target_key for item in self.targets]
        concept_ids = [item.concept_revision_id for item in self.targets]
        positions = [item.dependency_position for item in self.targets]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("M2 dependency targets must be distinct")
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("M2 dependency concepts must be distinct revisions")
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise ValueError("M2 dependency positions must be contiguous from one")
        scenarios = [item.scenario for item in self.failure_probes]
        if len(scenarios) != len(set(scenarios)):
            raise ValueError("duplicate M2 failure-close scenario")
        missing = REQUIRED_FAILURE_SCENARIOS - set(scenarios)
        if missing:
            raise ValueError(f"missing M2 failure-close scenarios: {sorted(missing)}")
        return self


class M2JourneyDriver(Protocol):
    def execute(self, authority: M2AuthoritySnapshot) -> M2JourneyExecution: ...


class M2FailureProbeRunner:
    """Inject the three M2-E2 invalid candidates without formal-content writes."""

    def __init__(self, db: Session, *, knowledge_package_path: Path):
        self.db = db
        self.knowledge_package_path = knowledge_package_path

    def _counts(self) -> tuple[int, int, int]:
        return (
            self.db.scalar(select(func.count()).select_from(ContentVersion)) or 0,
            self.db.scalar(select(func.count()).select_from(QuizSet)) or 0,
            self.db.scalar(select(func.count()).select_from(AssessmentObservation)) or 0,
        )

    def _probe(
        self,
        *,
        scenario: str,
        expected_codes: set[str],
        operation: Callable[[], None],
    ) -> M2FailureCloseProbe:
        before = self._counts()
        rejected = False
        error_code = "NO_REJECTION"
        try:
            operation()
        except AppError as error:
            rejected = True
            error_code = error.code
        after = self._counts()
        if not rejected or error_code not in expected_codes:
            raise RuntimeError(
                f"M2 failure probe {scenario} expected {sorted(expected_codes)}, "
                f"got {error_code}"
            )
        return M2FailureCloseProbe(
            scenario=scenario,
            rejected=True,
            errorCode=error_code,
            contentCountBefore=before[0],
            contentCountAfter=after[0],
            quizCountBefore=before[1],
            quizCountAfter=after[1],
            observationCountBefore=before[2],
            observationCountAfter=after[2],
            evidence=[
                f"failure-probe:{scenario}:{error_code}",
                f"formal-counts:{before[0]}/{before[1]}/{before[2]}",
            ],
        )

    def run(self) -> list[M2FailureCloseProbe]:
        facts = KnowledgeFactGraphService(self.db)
        source = facts.read_package(self.knowledge_package_path)
        raw = source.model_dump(by_alias=True, mode="json")

        outside_raw = deepcopy(raw)
        outside_raw["version"] = source.version + 1001
        outside_raw["concepts"][0]["objectiveKeys"].append(
            "m2_injected_outside_baseline_objective"
        )

        def outside_scope():
            from ..modules.knowledge.fact_graph import KnowledgeGraphSlicePackage

            facts.import_candidate(KnowledgeGraphSlicePackage.model_validate(outside_raw))

        unsupported_raw = deepcopy(raw)
        unsupported_raw["version"] = source.version + 1002
        claim_key_map = {
            item["key"]: f"{item['key']}__m2_unsupported_probe"
            for item in unsupported_raw["claims"]
        }
        for item in unsupported_raw["claims"]:
            item["key"] = claim_key_map[item["key"]]
        for item in unsupported_raw["concepts"]:
            item["claimKeys"] = [claim_key_map[key] for key in item["claimKeys"]]
        for item in unsupported_raw["relations"]:
            item["claimKeys"] = [claim_key_map[key] for key in item["claimKeys"]]
        for item in unsupported_raw.get("declaredGaps", []):
            if item.get("subjectKind") == "source_claim_version":
                item["subjectKey"] = claim_key_map[item["subjectKey"]]
        unsupported_raw["claims"][0]["sourceBindings"] = []

        def unsupported_claim():
            from ..modules.knowledge.fact_graph import KnowledgeGraphSlicePackage

            package = KnowledgeGraphSlicePackage.model_validate(unsupported_raw)
            candidate = facts.import_candidate(package)
            review = KnowledgeGraphReviewManifest.model_validate(
                {
                    "schemaVersion": "knowledge_graph_review_v1",
                    "releaseId": candidate.id,
                    "contentHash": candidate.content_hash,
                    "decision": "approved",
                    "reviewerId": "m2_failure_probe",
                    "reviewedAt": "2026-08-09T00:00:00Z",
                    "reviewNote": "synthetic rejection probe, never a publication approval",
                    "acceptedSourceKeys": [item.source_key for item in package.sources],
                    "acceptedClaimKeys": [item.key for item in package.claims],
                    "acceptedRelationKeys": [item.key for item in package.relations],
                    "gapDispositions": [],
                }
            )
            facts.publish(
                candidate.id,
                review=review,
            )

        mismatch_raw = deepcopy(raw)
        mismatch_raw["version"] = source.version + 1003
        mismatch_raw["baselineVersionId"] = "m2_injected_wrong_baseline_version"

        def version_mismatch():
            from ..modules.knowledge.fact_graph import KnowledgeGraphSlicePackage

            facts.import_candidate(KnowledgeGraphSlicePackage.model_validate(mismatch_raw))

        return [
            self._probe(
                scenario="baseline_out_of_scope",
                expected_codes={"KNOWLEDGE_GRAPH_BASELINE_SCOPE_VIOLATION"},
                operation=outside_scope,
            ),
            self._probe(
                scenario="knowledge_support_missing",
                expected_codes={
                    "KNOWLEDGE_GRAPH_BLOCKING_GAP",
                    "KNOWLEDGE_GRAPH_CLAIM_UNSUPPORTED",
                },
                operation=unsupported_claim,
            ),
            self._probe(
                scenario="version_mismatch",
                expected_codes={"KNOWLEDGE_GRAPH_BASELINE_NOT_FOUND"},
                operation=version_mismatch,
            ),
        ]


class M2HttpJourneyDriver:
    """Minimal public-API journey for the real-provider M2 dependency slice.

    ``answerer`` must be an independently configured provider (or another
    explicit learner strategy); it receives the opened section and quiz and
    returns the public API answer payload. ``failure_probe_runner`` performs
    the three invalid-candidate injections after the successful journey.
    """

    DEFAULT_TARGET_KEYS = ("recursion", "graph_search", "dynamic_programming")
    THIN_SLICE_OBJECTIVE_KEYS = frozenset(
        {
            "solve_with_enumeration_recursion_and_search",
            "model_and_solve_with_dynamic_programming",
        }
    )

    def __init__(
        self,
        *,
        client,
        db: Session,
        provider: str,
        model: str,
        answerer: Callable[[dict, dict], list[list[int]]],
        failure_probe_runner: Callable[[], list[M2FailureCloseProbe]],
        projection_rebuild_runner: Callable[[], list[str]],
        plan_input: dict[str, Any],
        target_concept_keys: tuple[str, str, str] = DEFAULT_TARGET_KEYS,
        timeout_seconds: int = 1500,
    ):
        self.client = client
        self.db = db
        self.provider = provider
        self.model = model
        self.answerer = answerer
        self.failure_probe_runner = failure_probe_runner
        self.projection_rebuild_runner = projection_rebuild_runner
        self.plan_input = dict(plan_input)
        self.target_concept_keys = target_concept_keys
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, *, body=None, expected=200):
        response = self.client.request(method, path, json=body)
        payload = response.json() if response.content else {}
        if response.status_code != expected:
            raise RuntimeError(
                f"{method} {path}: expected {expected}, got "
                f"{response.status_code}: {payload}"
            )
        return payload

    def _wait_tasks(self, payload: dict) -> None:
        for task in payload.get("workflowTasks", []):
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                state = self._request(
                    "GET", f"/api/learning-tasks/{task['taskId']}"
                )
                if state.get("status") == "succeeded":
                    break
                if state.get("status") == "failed":
                    raise RuntimeError(
                        "M2 workflow task failed: "
                        f"{state.get('type')} {state.get('errorCode')}"
                    )
                time.sleep(0.1)
            else:
                raise RuntimeError(f"M2 workflow task timed out: {task['taskId']}")

    def _initialize_series(self) -> dict:
        created = self._request("POST", "/api/plans", body=self.plan_input, expected=201)
        task = created.get("initializationTask")
        if task and task.get("status") not in {"succeeded", "failed"}:
            self._wait_tasks({"workflowTasks": [task]})
        elif task and task.get("status") == "failed":
            raise RuntimeError(
                f"M2 plan initialization failed: {task.get('errorCode')}"
            )
        return self._request("GET", f"/api/series/{created['id']}")

    def _target_rows_for_section(self, section_id: str):
        contract = self.db.scalar(
            select(LearningContractVersion)
            .where(LearningContractVersion.section_id == section_id)
            .order_by(LearningContractVersion.version.desc())
        )
        if not contract:
            return []
        rows = self.db.execute(
            select(LearningContractConcept, ConceptRevision, Concept)
            .join(
                ConceptRevision,
                ConceptRevision.id == LearningContractConcept.concept_revision_id,
            )
            .join(Concept, Concept.id == ConceptRevision.concept_id)
            .where(LearningContractConcept.contract_version_id == contract.id)
            .order_by(LearningContractConcept.position)
        ).all()
        return [
            (binding, revision, concept)
            for binding, revision, concept in rows
            if concept.concept_key in self.target_concept_keys
        ]

    def _assert_explicit_section_identity(self, section_id: str) -> None:
        section = self.db.get(Section, section_id)
        objectives = _load(section.objectives_json, []) if section else []
        declared = [
            item
            for item in objectives
            if isinstance(item, dict)
            and item.get("baselineConceptKey")
            and item.get("baselineObjectiveKey")
        ]
        # A file-SQLite acceptance run also serves HTTP writes from separate
        # sessions. End this read transaction before the lesson POST.
        self.db.rollback()
        if not declared:
            raise RuntimeError(
                "M2 section planning did not emit explicit baselineConceptKey and "
                f"baselineObjectiveKey; stopped before provider lesson call: {section_id}"
            )

    def _thin_slice_chapter_prefix(
        self,
        *,
        authority: M2AuthoritySnapshot,
        chapter_ids: list[str],
    ) -> list[str]:
        release = self.db.get(KnowledgeGraphRelease, authority.graph_release_id)
        manifest = _load(release.manifest_json, {}) if release else {}
        published_objectives = {
            item.objective_key
            for item_id in manifest.get("objectiveIds", [])
            if (item := self.db.get(LearningObjective, item_id)) is not None
        }
        prefix: list[str] = []
        covered_concepts: set[str] = set()
        for chapter_id in chapter_ids:
            chapter_objectives = set(
                self.db.scalars(
                    select(ChapterCurriculumObjectiveBinding.objective_key).where(
                        ChapterCurriculumObjectiveBinding.chapter_id == chapter_id,
                        ChapterCurriculumObjectiveBinding.baseline_version_id
                        == authority.baseline_version_id,
                    )
                ).all()
            )
            chapter = self.db.get(Chapter, chapter_id)
            identity_scope = _load(
                chapter.knowledge_identity_scope_json if chapter else "{}", {}
            )
            chapter_concepts = {
                str(item.get("conceptKey"))
                for item in identity_scope.get("pairs", [])
                if isinstance(item, dict) and item.get("conceptKey")
            }
            if (
                not chapter_objectives
                or not chapter_objectives <= published_objectives
                or not chapter_concepts
                or not chapter_concepts <= set(self.target_concept_keys)
            ):
                self.db.rollback()
                raise RuntimeError(
                    "M2 thin-slice chapters must precede objectives outside the "
                    f"published graph; chapter {chapter_id} got "
                    f"objectives={sorted(chapter_objectives)}, "
                    f"concepts={sorted(chapter_concepts)}"
                )
            prefix.append(chapter_id)
            covered_concepts.update(chapter_concepts)
            if covered_concepts == set(self.target_concept_keys):
                self.db.rollback()
                return prefix
        self.db.rollback()
        raise RuntimeError(
            "M2 first-book chapter prefix did not cover all thin-slice concepts; "
            f"got {sorted(covered_concepts)}"
        )

    def _objective_key_for_section(self, section_id: str, concept_key: str) -> str:
        section = self.db.get(Section, section_id)
        objective_keys = {
            str(item.get("baselineObjectiveKey"))
            for item in _load(section.objectives_json if section else "[]", [])
            if isinstance(item, dict)
            and item.get("baselineConceptKey") == concept_key
            and item.get("baselineObjectiveKey")
        }
        if len(objective_keys) != 1:
            raise RuntimeError(
                "M2 target section must freeze exactly one objective key for "
                f"concept {concept_key}; got {sorted(objective_keys)}"
            )
        return next(iter(objective_keys))

    def _execution_refs(
        self,
        *,
        authority: M2AuthoritySnapshot,
        section_id: str,
        concept_revision: ConceptRevision,
        concept: Concept,
        dependency_position: int,
    ) -> M2TargetExecution:
        run = self.db.scalar(
            select(GenerationRun)
            .where(
                GenerationRun.section_id == section_id,
                GenerationRun.status == "succeeded",
                GenerationRun.operation.in_(("lesson", "regeneration")),
            )
            .order_by(GenerationRun.attempt.desc())
        )
        if not run:
            raise RuntimeError(f"M2 target has no successful generation run: {section_id}")
        content = self.db.scalar(
            select(ContentVersion)
            .where(
                ContentVersion.section_id == section_id,
                ContentVersion.generation_run_id == run.id,
                ContentVersion.publication_status == "published",
            )
            .order_by(ContentVersion.version.desc())
        )
        quiz = (
            self.db.scalar(
                select(QuizSet)
                .where(
                    QuizSet.content_version_id == content.id,
                    QuizSet.publication_status == "published",
                )
                .order_by(QuizSet.generation.desc())
            )
            if content
            else None
        )
        passed_attempt = (
            self.db.scalar(
                select(QuizAttempt)
                .where(
                    QuizAttempt.quiz_set_id == quiz.id,
                    QuizAttempt.content_version_id == content.id,
                    QuizAttempt.passed.is_(True),
                )
                .order_by(QuizAttempt.created_at.desc())
            )
            if content and quiz
            else None
        )
        observations = (
            self.db.scalars(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.section_id == section_id,
                    AssessmentObservation.content_version_id == content.id,
                    AssessmentObservation.quiz_set_id == quiz.id,
                    AssessmentObservation.attempt_id == passed_attempt.id,
                )
                .order_by(AssessmentObservation.sequence)
            ).all()
            if content and quiz and passed_attempt
            else []
        )
        trace = _load(run.trace_json, {})
        pack = trace.get("knowledgeContext", {})
        if not content or not quiz or not observations:
            raise RuntimeError(f"M2 target evidence chain is incomplete: {section_id}")
        return M2TargetExecution(
            targetKey=concept.concept_key,
            baselineObjectiveKey=self._objective_key_for_section(
                section_id, concept.concept_key
            ),
            conceptRevisionId=concept_revision.id,
            dependencyPosition=dependency_position,
            generationRunId=run.id,
            contentVersionId=content.id,
            quizSetId=quiz.id,
            observationIds=[item.id for item in observations],
            knowledgeContext=pack,
            knowledgeDimensionCompleted=bool(passed_attempt and observations),
            codeCapabilityVerified=False,
            codeEvidenceIds=[],
        )

    def execute(self, authority: M2AuthoritySnapshot) -> M2JourneyExecution:
        series = self._initialize_series()
        binding = self.db.scalar(
            select(SeriesCurriculumBaselineBinding).where(
                SeriesCurriculumBaselineBinding.series_id == series["id"]
            )
        )
        bound_baseline_id = binding.baseline_version_id if binding else ""
        self.db.rollback()
        if bound_baseline_id != authority.baseline_version_id:
            raise RuntimeError("M2 plan did not adopt the accepted baseline version")
        books = series.get("books", [])
        first_book_chapters = books[0].get("chapters", []) if books else []
        first_book_chapter_ids = [
            item.get("id") for item in first_book_chapters if item.get("id")
        ]
        if not first_book_chapter_ids:
            raise RuntimeError("M2 plan has no first chapter")
        thin_slice_chapter_ids = set(self._thin_slice_chapter_prefix(
            authority=authority,
            chapter_ids=first_book_chapter_ids,
        ))

        collected: dict[str, M2TargetExecution] = {}
        for book in series.get("books", []):
            for chapter_summary in book.get("chapters", []):
                if chapter_summary["id"] not in thin_slice_chapter_ids:
                    continue
                chapter = self._request(
                    "POST", f"/api/chapters/{chapter_summary['id']}/generate"
                )
                for section_summary in chapter.get("sections", []):
                    section_id = section_summary["id"]
                    self.db.expire_all()
                    self._assert_explicit_section_identity(section_id)
                    if section_summary.get("status") == "completed":
                        opened = self._request("GET", f"/api/sections/{section_id}")
                    else:
                        self._request("POST", f"/api/sections/{section_id}/generate")
                        opened = self._request("POST", f"/api/sections/{section_id}/open")
                        quiz = opened["quiz"]
                        result = None
                        for _learner_attempt in range(3):
                            answers = self.answerer(opened, quiz)
                            if len(answers) != len(quiz["questions"]):
                                continue
                            result = self._request(
                                "POST",
                                f"/api/sections/{section_id}/quiz",
                                body={"quizSetId": quiz["id"], "answers": answers},
                            )
                            if result.get("passed"):
                                break
                        if not result or not result.get("passed"):
                            raise RuntimeError(
                                "M2 independent learner did not pass quiz within "
                                "three attempts"
                            )
                        self._wait_tasks(result)
                    self.db.expire_all()
                    for _binding, revision, concept in self._target_rows_for_section(
                        section_id
                    ):
                        if concept.concept_key in collected:
                            continue
                        collected[concept.concept_key] = self._execution_refs(
                            authority=authority,
                            section_id=section_id,
                            concept_revision=revision,
                            concept=concept,
                            dependency_position=(
                                self.target_concept_keys.index(concept.concept_key) + 1
                            ),
                        )
                    # Do not hold a read lock or ORM identity snapshot across
                    # the next public-API write.
                    self.db.rollback()
                    if set(collected) == set(self.target_concept_keys):
                        break
                if set(collected) == set(self.target_concept_keys):
                    break
            if set(collected) == set(self.target_concept_keys):
                break
        missing = sorted(set(self.target_concept_keys) - set(collected))
        if missing:
            raise RuntimeError(f"M2 real journey missed dependency targets: {missing}")
        probes = self.failure_probe_runner()
        rebuild_evidence = self.projection_rebuild_runner()
        return M2JourneyExecution(
            executionMode="real_provider",
            provider=self.provider,
            model=self.model,
            seriesId=series["id"],
            baselineVersionId=authority.baseline_version_id,
            graphReleaseId=authority.graph_release_id,
            targets=[collected[key] for key in self.target_concept_keys],
            failureProbes=probes,
            projectionRebuildEvidence=rebuild_evidence,
        )


class M2AcceptanceAudit(RunnerModel):
    baseline_source_evidence: list[str] = Field(
        alias="baselineSourceEvidence", min_length=1
    )
    baseline_publication_evidence: list[str] = Field(
        alias="baselinePublicationEvidence", min_length=1
    )
    code_policy_evidence: list[str] = Field(alias="codePolicyEvidence", min_length=1)
    graph_identity_evidence: list[str] = Field(alias="graphIdentityEvidence", min_length=1)
    graph_claim_evidence: list[str] = Field(alias="graphClaimEvidence", min_length=1)
    context_pack_evidence: list[str] = Field(alias="contextPackEvidence", min_length=1)
    generation_audit_evidence: list[str] = Field(
        alias="generationAuditEvidence", min_length=1
    )
    journey_evidence: list[str] = Field(alias="journeyEvidence", min_length=1)
    failure_close_evidence: list[str] = Field(
        alias="failureCloseEvidence", min_length=1
    )
    errors: dict[str, list[str]] = Field(default_factory=dict)


class M2EvidenceAuditor(Protocol):
    def audit(
        self,
        authority: M2AuthoritySnapshot,
        execution: M2JourneyExecution,
    ) -> M2AcceptanceAudit: ...


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class SqlAlchemyM2EvidenceAuditor:
    """Read-only audit of the isolated acceptance database."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _add(errors: dict[str, list[str]], gate_id: str, condition: bool, message: str):
        if not condition:
            errors.setdefault(gate_id, []).append(message)

    def audit(
        self,
        authority: M2AuthoritySnapshot,
        execution: M2JourneyExecution,
    ) -> M2AcceptanceAudit:
        errors: dict[str, list[str]] = {}
        add = self._add
        baseline = self.db.get(CurriculumBaselineVersion, authority.baseline_version_id)
        release = self.db.get(KnowledgeGraphRelease, authority.graph_release_id)

        review = _load(baseline.review_json, {}) if baseline else {}
        graph = _load(baseline.graph_json, {}) if baseline else {}
        source_ids = _load(baseline.source_version_ids_json, []) if baseline else []
        curriculum_sources = [
            self.db.get(CurriculumSourceVersion, item) for item in source_ids
        ]
        add(errors, "M2-B1", baseline is not None, "baseline row is missing")
        add(errors, "M2-B1", bool(source_ids), "baseline has no frozen source versions")
        add(
            errors,
            "M2-B1",
            bool(curriculum_sources)
            and all(
                item is not None
                and item.verification_status == "reviewed"
                and item.url
                and item.title
                and item.authority
                and item.version_label
                and item.retrieval_date
                and item.content_digest
                and bool(_load(item.applicability_json, {}))
                and bool(_load(item.provenance_json, {}))
                for item in curriculum_sources
            ),
            "curriculum sources lack review, version, applicability, provenance or digest",
        )
        add(
            errors,
            "M2-B2",
            baseline is not None
            and baseline.status == "published"
            and review.get("schemaVersion") == "curriculum_baseline_review_v1"
            and review.get("finalDecision") == "approved",
            "baseline is not published from an approved review manifest",
        )
        series_binding = self.db.scalar(
            select(SeriesCurriculumBaselineBinding).where(
                SeriesCurriculumBaselineBinding.series_id == execution.series_id
            )
        )
        add(
            errors,
            "M2-B2",
            series_binding is not None
            and series_binding.baseline_version_id == authority.baseline_version_id,
            "journey series is not frozen to the accepted baseline version",
        )
        required_objectives = {
            item.get("key")
            for item in graph.get("objectives", [])
            if item.get("required") and item.get("key")
        }
        covered_objectives = set(
            self.db.scalars(
                select(ChapterCurriculumObjectiveBinding.objective_key)
                .join(Chapter, Chapter.id == ChapterCurriculumObjectiveBinding.chapter_id)
                .join(Book, Book.id == Chapter.book_id)
                .where(
                    Book.series_id == execution.series_id,
                    ChapterCurriculumObjectiveBinding.baseline_version_id
                    == authority.baseline_version_id,
                )
            ).all()
        )
        add(
            errors,
            "M2-B2",
            bool(required_objectives) and required_objectives <= covered_objectives,
            "planned chapters do not cover every required baseline objective",
        )

        code_policy = review.get("platformCodeAssessment") or {}
        dimensions = code_policy.get("dimensions", [])
        course = (
            self.db.get(CourseVersion, baseline.course_version_id) if baseline else None
        )
        assessment = _load(course.assessment_json, {}) if course else {}
        required_evidence_modes = set(assessment.get("requiredEvidenceModes", []))
        # The review policy is platform-owned and deliberately not represented
        # as an official PKU grading standard.
        add(
            errors,
            "M2-B3",
            code_policy.get("authority") == "slow_platform"
            and code_policy.get("officialCoursePolicy") is False
            and sum(int(item.get("weight", 0)) for item in dimensions) == 100
            and bool(code_policy.get("requiredDimensionMinimums")),
            "platform code rubric is absent, malformed, or presented as official",
        )
        add(
            errors,
            "M2-B3",
            "code_task" in required_evidence_modes,
            "course assessment does not explicitly require separate code-task evidence",
        )
        add(
            errors,
            "M2-B3",
            all(
                not target.code_capability_verified or bool(target.code_evidence_ids)
                for target in execution.targets
            ),
            "choice-quiz evidence was incorrectly promoted to code capability evidence",
        )

        manifest = _load(release.manifest_json, {}) if release else {}
        concept_ids = manifest.get("conceptRevisionIds", [])
        objective_ids = manifest.get("objectiveIds", [])
        relation_ids = manifest.get("relationVersionIds", [])
        claim_ids = manifest.get("claimVersionIds", [])
        source_version_ids = manifest.get("sourceVersionIds", [])
        claim_binding_ids = manifest.get("claimBindingIds", [])
        objective_binding_ids = manifest.get("conceptObjectiveBindingIds", [])
        concepts = [self.db.get(ConceptRevision, item) for item in concept_ids]
        objectives = [self.db.get(LearningObjective, item) for item in objective_ids]
        relations = [self.db.get(ConceptRelationVersion, item) for item in relation_ids]
        claims = [self.db.get(SourceClaimVersion, item) for item in claim_ids]
        knowledge_sources = [
            self.db.get(KnowledgeSourceVersion, item) for item in source_version_ids
        ]
        claim_bindings = [
            self.db.get(KnowledgeClaimBinding, item) for item in claim_binding_ids
        ]
        objective_bindings = [
            self.db.get(ConceptObjectiveBinding, item) for item in objective_binding_ids
        ]
        add(
            errors,
            "M2-C1",
            release is not None
            and release.status == "published"
            and release.baseline_version_id == authority.baseline_version_id,
            "knowledge graph is not a published release of the accepted baseline",
        )
        add(
            errors,
            "M2-C1",
            len(concepts) >= 3
            and all(
                item is not None and item.verification_status == "reviewed"
                for item in concepts
            )
            and bool(objectives)
            and all(
                item is not None and item.verification_status == "reviewed"
                for item in objectives
            )
            and bool(relations)
            and all(item is not None and item.status == "published" for item in relations)
            and bool(objective_bindings)
            and all(item is not None for item in objective_bindings),
            "stable concepts, objectives, typed relations or bindings are incomplete",
        )
        supporting_claim_ids = {
            item.source_claim_version_id
            for item in claim_bindings
            if item is not None
            and item.support_type in {"supports", "defines"}
            and item.verification_status in {"verified", "cross_source"}
            and bool(_load(item.locator_json, {}))
        }
        add(
            errors,
            "M2-C2",
            bool(claims)
            and all(
                item is not None
                and item.status == "published"
                and item.trust_state == "verified"
                and item.id in supporting_claim_ids
                for item in claims
            )
            and bool(knowledge_sources)
            and all(
                item is not None
                and item.verification_status in {"reviewed", "verified"}
                and item.rights_status in {"public", "open_access", "licensed"}
                for item in knowledge_sources
            )
            and not any(
                item.get("severity") == "blocking"
                for item in (_load(release.gaps_json, []) if release else [])
            ),
            "published claims do not all have reviewed, locatable, publishable support",
        )

        ordered_targets = sorted(
            execution.targets, key=lambda item: item.dependency_position
        )
        physical_invocations = []
        if execution.execution_mode == "real_provider":
            physical_invocations = self.db.scalars(
                select(AiInvocation).where(
                    AiInvocation.operation.in_(("lesson_generation_v2", "lesson_generation_v3")),
                    AiInvocation.status == "succeeded",
                    AiInvocation.provider == execution.provider,
                    AiInvocation.model == execution.model,
                )
            ).all()
            add(
                errors,
                "M2-E1",
                len(physical_invocations)
                >= len({item.generation_run_id for item in ordered_targets})
                and all(item.provider_response_id for item in physical_invocations),
                "real-provider physical invocation evidence is incomplete",
            )
        target_concept_ids = {item.concept_revision_id for item in ordered_targets}
        induced_pairs = {
            frozenset(
                {item.from_concept_revision_id, item.to_concept_revision_id}
            )
            for item in relations
            if item is not None
            and item.from_concept_revision_id in target_concept_ids
            and item.to_concept_revision_id in target_concept_ids
        }
        reachable = set()
        frontier = [ordered_targets[0].concept_revision_id] if ordered_targets else []
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(
                list(pair - {current})[0]
                for pair in induced_pairs
                if current in pair and not pair <= reachable
            )
        add(
            errors,
            "M2-E1",
            len(induced_pairs) >= 2 and reachable == target_concept_ids,
            "three acceptance targets do not form a connected reviewed relation slice",
        )

        context_evidence: list[str] = []
        generation_evidence: list[str] = []
        journey_evidence: list[str] = []
        for target in ordered_targets:
            pack = target.knowledge_context
            budget = pack.get("budget", {})
            actual = pack.get("actual", {})
            add(
                errors,
                "M2-D1",
                pack.get("schemaVersion") == "knowledge_context_pack_v1"
                and pack.get("status") == "ready"
                and pack.get("releaseId") == authority.graph_release_id
                and pack.get("baselineVersionId") == authority.baseline_version_id
                and pack.get("retrievalRuleVersion") == "published_bounded_bfs_v1"
                and bool(pack.get("contextHash"))
                and bool(pack.get("seedConceptRevisionIds"))
                and int(actual.get("nodeCount", -1)) <= int(budget.get("maxNodes", -2))
                and int(actual.get("edgeCount", -1)) <= int(budget.get("maxEdges", -2))
                and len(pack.get("nodeRevisionIds", []))
                == int(actual.get("nodeCount", -1))
                and len(pack.get("relationVersionIds", []))
                == int(actual.get("edgeCount", -1))
                and len(pack.get("claimVersionIds", []))
                == int(actual.get("claimCount", -1))
                and "maxHops" in budget
                and "truncation" in pack,
                f"target {target.target_key} lacks a valid bounded knowledge context",
            )
            run = self.db.get(GenerationRun, target.generation_run_id)
            trace = _load(run.trace_json, {}) if run else {}
            trace_context = trace.get("knowledgeContext", {})
            add(
                errors,
                "M2-D2",
                run is not None
                and run.status == "succeeded"
                and trace_context.get("contextHash") == pack.get("contextHash")
                and trace_context.get("releaseId") == authority.graph_release_id
                and trace_context.get("budget") == budget
                and trace_context.get("actual") == actual
                and trace_context.get("truncation") == pack.get("truncation"),
                f"generation run {target.generation_run_id} has incomplete context audit",
            )
            content = self.db.get(ContentVersion, target.content_version_id)
            quiz = self.db.get(QuizSet, target.quiz_set_id)
            observations = [
                self.db.scalar(
                    select(AssessmentObservation).where(
                        AssessmentObservation.id == observation_id
                    )
                )
                for observation_id in target.observation_ids
            ]
            add(
                errors,
                "M2-E1",
                target.knowledge_dimension_completed
                and run is not None
                and run.status == "succeeded"
                and content is not None
                and content.publication_status == "published"
                and content.generation_run_id == run.id
                and quiz is not None
                and quiz.publication_status == "published"
                and quiz.content_version_id == content.id
                and bool(observations)
                and all(
                    item is not None
                    and item.quiz_set_id == quiz.id
                    and item.content_version_id == content.id
                    for item in observations
                ),
                f"target {target.target_key} is missing published or observation facts",
            )
            if execution.execution_mode == "real_provider":
                add(
                    errors,
                    "M2-E1",
                    bool(execution.provider)
                    and bool(execution.model)
                    and bool(run and run.model)
                    and bool(run and run.generation_mode != "demo"),
                    f"target {target.target_key} is not attributable to a real provider",
                )
            context_evidence.append(
                f"knowledge-context:{target.target_key}:{pack.get('contextHash', '')}"
            )
            generation_evidence.append(f"generation-run:{target.generation_run_id}")
            journey_evidence.extend(
                [
                    f"content:{target.content_version_id}",
                    f"quiz:{target.quiz_set_id}",
                    *[f"observation:{item}" for item in target.observation_ids],
                ]
            )

        add(
            errors,
            "M2-E2",
            all(item.failed_closed for item in execution.failure_probes),
            "at least one injected invalid candidate wrote authoritative facts",
        )
        failure_evidence = [
            evidence
            for probe in execution.failure_probes
            for evidence in probe.evidence
        ]
        return M2AcceptanceAudit(
            baselineSourceEvidence=[
                f"curriculum-source:{item.id}:{item.content_digest}"
                for item in curriculum_sources
                if item is not None
            ]
            or ["missing:curriculum-source"],
            baselinePublicationEvidence=[
                f"curriculum-baseline:{authority.baseline_version_id}:{authority.baseline_content_hash}",
                f"series-binding:{execution.series_id}:{authority.baseline_version_id}",
            ],
            codePolicyEvidence=[
                f"platform-code-policy:{code_policy.get('policyKey', 'missing')}"
            ],
            graphIdentityEvidence=[
                f"knowledge-graph:{authority.graph_release_id}:{authority.graph_content_hash}",
                *[f"concept-revision:{item}" for item in concept_ids],
                *[f"relation-version:{item}" for item in relation_ids],
            ],
            graphClaimEvidence=[
                *[f"claim-version:{item}" for item in claim_ids],
                *[f"claim-binding:{item}" for item in claim_binding_ids],
            ]
            or ["missing:claim-evidence"],
            contextPackEvidence=context_evidence or ["missing:knowledge-context"],
            generationAuditEvidence=generation_evidence or ["missing:generation-run"],
            journeyEvidence=[
                *journey_evidence,
                *[
                    f"provider-invocation:{item.id}:{item.provider_response_id}"
                    for item in physical_invocations
                ],
                *execution.projection_rebuild_evidence,
            ],
            failureCloseEvidence=failure_evidence or ["missing:failure-close-probe"],
            errors=errors,
        )


@dataclass(frozen=True)
class M2RunnerInputs:
    run_id: str
    code_revision: str
    baseline_package_path: Path
    baseline_review_path: Path
    knowledge_package_path: Path
    knowledge_review_path: Path
    historical_a1_evidence: tuple[str, ...]
    historical_a2_evidence: tuple[str, ...]
    baseline_publication_note: str = "M2 v2 acceptance reviewed publication"


@dataclass(frozen=True)
class M2RunnerResult:
    authority: M2AuthoritySnapshot
    execution: M2JourneyExecution
    audit: M2AcceptanceAudit
    acceptance: M2AcceptanceEvidence

    def json_payload(self) -> dict:
        payload = self.acceptance.model_dump(by_alias=True, mode="json")
        return {
            **payload,
            "decision": self.acceptance.decision,
            "blockingGateIds": self.acceptance.blocking_gate_ids,
            "authority": self.authority.model_dump(by_alias=True, mode="json"),
            "execution": self.execution.model_dump(by_alias=True, mode="json"),
            "audit": self.audit.model_dump(by_alias=True, mode="json"),
        }

    def markdown(self) -> str:
        status = self.acceptance.decision
        lines = [
            f"# M2 v2 验收报告：{status}",
            "",
            f"- Run ID：`{self.acceptance.run_id}`",
            f"- 代码版本：`{self.acceptance.code_revision}`",
            f"- 课程基准：`{self.authority.baseline_version_id}` v{self.authority.baseline_version}",
            f"- 知识图发布：`{self.authority.graph_release_id}` v{self.authority.graph_release_version}",
            f"- 执行模式：`{self.execution.execution_mode}`",
            f"- Provider / Model：`{self.execution.provider}` / `{self.execution.model}`",
            "",
            "## 硬门禁",
            "",
            "| 门禁 | 状态 | 证据 | Findings |",
            "| --- | --- | --- | --- |",
        ]
        for gate in self.acceptance.gates:
            evidence = "<br>".join(gate.evidence) or "—"
            findings = "<br>".join(gate.findings) or "—"
            lines.append(f"| {gate.gate_id} | {gate.status} | {evidence} | {findings} |")
        lines.extend(
            [
                "",
                "## 能力证据边界",
                "",
                "本次小节选择题只验证知识维度。除非另有已评分的代码或章末实践证据，"
                "不得据此声称程序设计能力已经验证。",
                "",
            ]
        )
        return "\n".join(lines)

    def write(self, *, json_path: Path, markdown_path: Path) -> None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(self.json_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(self.markdown(), encoding="utf-8")


class M2AcceptanceRunner:
    def __init__(
        self,
        db: Session,
        *,
        driver: M2JourneyDriver,
        auditor: M2EvidenceAuditor,
    ):
        self.db = db
        self.driver = driver
        self.auditor = auditor

    def install_authority(self, inputs: M2RunnerInputs) -> M2AuthoritySnapshot:
        baselines = CurriculumBaselineService(self.db)
        package = baselines.read_package(inputs.baseline_package_path)
        review = baselines.read_review(inputs.baseline_review_path)
        baseline = baselines.import_candidate(package)
        if baseline.status == "candidate":
            baseline = baselines.apply_review(baseline.id, review)
            baseline = baselines.publish(
                baseline.id,
                reviewer_id=review.reviewer_id,
                review_note=inputs.baseline_publication_note,
            )
        if baseline.status != "published":
            raise AppError(
                "M2 验收只能使用已发布课程基准",
                code="M2_BASELINE_NOT_PUBLISHED",
                status=409,
            )

        facts = KnowledgeFactGraphService(self.db)
        graph_package = facts.read_package(inputs.knowledge_package_path)
        graph_review = facts.read_review(inputs.knowledge_review_path)
        if graph_package.baseline_version_id != baseline.id:
            raise AppError(
                "M2 知识图 package 与冻结课程基准版本不一致",
                code="M2_KNOWLEDGE_PACKAGE_BASELINE_MISMATCH",
                status=409,
                details={
                    "expectedBaselineVersionId": baseline.id,
                    "actualBaselineVersionId": graph_package.baseline_version_id,
                },
            )
        release = facts.import_candidate(graph_package)
        if release.status == "candidate":
            release = facts.publish(
                release.id,
                review=graph_review,
            )
        if release.status != "published":
            raise AppError(
                "M2 验收只能使用已发布知识图",
                code="M2_KNOWLEDGE_GRAPH_NOT_PUBLISHED",
                status=409,
            )
        return self._snapshot(baseline, release)

    @staticmethod
    def _snapshot(
        baseline: CurriculumBaselineVersion,
        release: KnowledgeGraphRelease,
    ) -> M2AuthoritySnapshot:
        return M2AuthoritySnapshot(
            baselineVersionId=baseline.id,
            baselineVersion=baseline.version,
            baselineContentHash=baseline.content_hash,
            graphReleaseId=release.id,
            graphReleaseVersion=release.version,
            graphContentHash=release.content_hash,
        )

    def run(self, inputs: M2RunnerInputs) -> M2RunnerResult:
        if not inputs.historical_a1_evidence or not inputs.historical_a2_evidence:
            raise ValueError("M2-A historical evidence references are required")
        authority = self.install_authority(inputs)
        execution = self.driver.execute(authority)
        if execution.baseline_version_id != authority.baseline_version_id:
            raise AppError(
                "真实旅程使用了错误的课程基准版本",
                code="M2_JOURNEY_BASELINE_VERSION_MISMATCH",
                status=409,
            )
        if execution.graph_release_id != authority.graph_release_id:
            raise AppError(
                "真实旅程使用了错误的知识图版本",
                code="M2_JOURNEY_GRAPH_VERSION_MISMATCH",
                status=409,
            )
        audit = self.auditor.audit(authority, execution)
        acceptance = self._acceptance(inputs, execution, audit)
        return M2RunnerResult(
            authority=authority,
            execution=execution,
            audit=audit,
            acceptance=acceptance,
        )

    @staticmethod
    def _acceptance(
        inputs: M2RunnerInputs,
        execution: M2JourneyExecution,
        audit: M2AcceptanceAudit,
    ) -> M2AcceptanceEvidence:
        evidence_by_gate = {
            "M2-A1": list(inputs.historical_a1_evidence),
            "M2-A2": list(inputs.historical_a2_evidence),
            "M2-B1": audit.baseline_source_evidence,
            "M2-B2": audit.baseline_publication_evidence,
            "M2-B3": audit.code_policy_evidence,
            "M2-C1": audit.graph_identity_evidence,
            "M2-C2": audit.graph_claim_evidence,
            "M2-D1": audit.context_pack_evidence,
            "M2-D2": audit.generation_audit_evidence,
            "M2-E1": audit.journey_evidence,
            "M2-E2": audit.failure_close_evidence,
        }
        gates = []
        for gate_id in M2_HARD_GATES:
            errors = list(audit.errors.get(gate_id, []))
            # Deterministic fixtures verify orchestration but are never real M2-E
            # evidence and therefore cannot yield an overall PASS.
            if gate_id == "M2-E1" and execution.execution_mode != "real_provider":
                status = "not_run"
                errors.append("fixture execution is not real-provider acceptance evidence")
            elif gate_id == "M2-E2" and not all(
                item.failed_closed for item in execution.failure_probes
            ):
                status = "fail"
                errors.append("one or more failure-close probes wrote authoritative facts")
            else:
                status = "fail" if errors else "pass"
            gates.append(
                M2GateResult(
                    gateId=gate_id,
                    status=status,
                    evidence=list(evidence_by_gate[gate_id]),
                    findings=errors,
                )
            )
        return M2AcceptanceEvidence(
            runId=inputs.run_id,
            codeRevision=inputs.code_revision,
            gates=gates,
        )
