import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    Base,
    CapabilityRevision,
    CapabilityRouteBinding,
    CapabilityApplicationEvaluation,
    CapabilityApplicationSubmission,
    CapabilityApplicationTaskVersion,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    Concept,
    ConceptRevision,
    ContentBlockVersion,
    ContentVersion,
    EvidenceQualificationEvent,
    KnowledgeNodeStateProjection,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    LearningRunSectionBinding,
    SectionAssessmentTarget,
    now,
)
from app.ai.contracts import (
    StandardApplicationCriterionResult,
    StandardApplicationEvaluation,
    StandardApplicationRubricCriterion,
    StandardApplicationTaskCandidate,
)
from app.modules.learning.assessment import (
    QUALIFICATION_RULE_VERSION,
    record_ask_me_assessment_facts,
    rebuild_assessment_projections,
)
from app.modules.learning.capabilities import (
    ensure_ask_me_stage_targets,
    ensure_route_capability,
)
from app.modules.learning.application_tasks import CapabilityApplicationTaskService


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _add_concept(db: Session) -> ConceptRevision:
    db.add(
        Concept(
            id="concept_recursion",
            namespace="test",
            concept_key="recursion",
            canonical_name="递归",
            status="active",
            origin="test",
        )
    )
    revision = ConceptRevision(
        id="revision_recursion",
        concept_id="concept_recursion",
        revision=1,
        label="递归",
        definition="通过调用自身解决可递归分解的问题。",
        scope_json=json.dumps(
            {
                "rankPolicy": {
                    "version": "knowledge_rank_policy_v1",
                    "capabilityScope": "把递归迁移到陌生问题",
                    "rankCeiling": "diamond",
                    "dimensionRanks": {"transfer": "diamond"},
                }
            }
        ),
        verification_status="route_scoped",
    )
    db.add(revision)
    db.flush()
    return revision


def _add_target(
    db: Session,
    *,
    target_id: str,
    capability_revision_id: str,
    criterion_id: str,
    dimension: str = "transfer",
    position: int = 1,
) -> AssessmentTarget:
    target = AssessmentTarget(
        id=target_id,
        concept_revision_id="revision_recursion",
        capability_revision_id=capability_revision_id,
        capability_stage_criterion_id=criterion_id,
        objective_key=f"key_{target_id}",
        objective_statement="识别递归的核心含义",
        dimension=dimension,
        target_depth="standard",
        identity_status="route_scoped_knowledge",
        status="active",
    )
    db.add(target)
    db.add(
        SectionAssessmentTarget(
            id=f"binding_{target_id}",
            section_id="section_capability",
            assessment_target_id=target_id,
            position=position,
            required=True,
            verification_policy="choice_quiz_v1",
        )
    )
    return target


def _add_observation(
    db: Session,
    *,
    suffix: str,
    target_id: str,
    episode_id: str | None = None,
) -> AssessmentObservation:
    observed_at = now()
    observation = AssessmentObservation(
        id=f"observation_{suffix}",
        learning_run_id="run_capability",
        user_id="user_capability",
        section_id="section_capability",
        attempt_id=f"attempt_{suffix}",
        scoring_result_id=f"scoring_{suffix}",
        assessment_target_id=target_id,
        question_index=0,
        correct=True,
        source_type="choice_quiz",
        assistance_mode="unassisted_initial",
        learning_episode_id=episode_id or f"episode_{suffix}",
        equivalence_group_id=f"equivalence_{suffix}",
        qualification_at_creation="eligible",
        qualification_rule_version=QUALIFICATION_RULE_VERSION,
        payload_json="{}",
        created_at=observed_at,
    )
    db.add(observation)
    db.flush()
    for family, status in {
        "gate": "eligible",
        "mastery": "eligible_grouped",
        "retention": "ineligible",
        "rank": "eligible_grouped",
        "capability": "eligible_grouped",
    }.items():
        db.add(
            EvidenceQualificationEvent(
                id=f"qualification_{suffix}_{family}",
                observation_id=observation.id,
                projection_family=family,
                status=status,
                reason="test qualification",
                rule_version=QUALIFICATION_RULE_VERSION,
                created_at=observed_at,
            )
        )
    return observation


def test_choice_transfer_evidence_is_diamond_in_legacy_but_bronze_in_capability_profile() -> None:
    with _session() as db:
        _add_concept(db)
        capability, bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        _add_target(
            db,
            target_id="target_transfer_choice",
            capability_revision_id=capability.id,
            criterion_id=bronze.id,
        )
        _add_observation(
            db,
            suffix="transfer_choice",
            target_id="target_transfer_choice",
        )
        db.flush()

        report = rebuild_assessment_projections(db, user_id="user_capability")
        old_state = db.scalar(select(KnowledgeNodeStateProjection))
        new_state = db.scalar(select(CapabilityStateProjection))

        assert old_state.current_rank == "diamond"
        assert new_state.current_stage == "bronze"
        assert new_state.highest_stage == "bronze"
        assert report["capabilityStates"] == 1
        assert report["qualifiedCapabilityObservations"] == 1


def test_cumulative_projection_cannot_skip_missing_bronze_criterion() -> None:
    with _session() as db:
        _add_concept(db)
        capability, bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        capability.natural_stage_ceiling = "silver"
        route = db.scalar(
            select(CapabilityRouteBinding).where(
                CapabilityRouteBinding.capability_revision_id == capability.id
            )
        )
        route.target_stage = "silver"
        silver_criteria = db.scalars(
            select(CapabilityStageCriterion).where(
                CapabilityStageCriterion.capability_revision_id == capability.id,
                CapabilityStageCriterion.stage == "silver",
            ).order_by(CapabilityStageCriterion.position)
        ).all()
        silver = silver_criteria[0]
        _add_target(
            db,
            target_id="target_silver",
            capability_revision_id=capability.id,
            criterion_id=silver.id,
            position=1,
        )
        _add_observation(db, suffix="silver_only", target_id="target_silver")
        db.flush()

        rebuild_assessment_projections(db, user_id="user_capability")
        state = db.scalar(select(CapabilityStateProjection))

        assert state.current_stage == "unranked"
        assert json.loads(state.satisfied_criterion_ids_json) == [silver.id]
        assert set(json.loads(state.missing_criterion_ids_json)) == {
            bronze.id,
            silver_criteria[1].id,
        }

        _add_target(
            db,
            target_id="target_bronze",
            capability_revision_id=capability.id,
            criterion_id=bronze.id,
            dimension="recognition",
            position=2,
        )
        _add_observation(db, suffix="bronze_after", target_id="target_bronze")
        db.flush()

        rebuild_assessment_projections(db, user_id="user_capability")
        db.refresh(state)

        assert state.current_stage == "bronze"
        assert json.loads(state.missing_criterion_ids_json) == [
            silver_criteria[1].id
        ]

        _add_target(
            db,
            target_id="target_silver_boundary",
            capability_revision_id=capability.id,
            criterion_id=silver_criteria[1].id,
            dimension="boundary",
            position=3,
        )
        _add_observation(
            db,
            suffix="silver_boundary",
            target_id="target_silver_boundary",
        )
        db.flush()
        rebuild_assessment_projections(db, user_id="user_capability")
        db.refresh(state)

        assert state.current_stage == "silver"
        assert state.current_stage_order == 2
        assert json.loads(state.missing_criterion_ids_json) == []
        assert state.independent_evidence_count == 3


def test_same_concept_reuses_capability_within_series_but_not_across_series() -> None:
    with _session() as db:
        _add_concept(db)
        first, first_bronze = ensure_route_capability(
            db,
            series_id="series_one",
            concept_revision_id="revision_recursion",
        )
        repeated, repeated_bronze = ensure_route_capability(
            db,
            series_id="series_one",
            concept_revision_id="revision_recursion",
        )
        other_series, _ = ensure_route_capability(
            db,
            series_id="series_two",
            concept_revision_id="revision_recursion",
        )

        assert repeated.id == first.id
        assert repeated_bronze.id == first_bronze.id
        assert other_series.id != first.id


def _add_ask_me_contract(
    db: Session,
    *,
    capability_revision_id: str,
) -> tuple[LearningContractVersion, dict[str, AssessmentTarget]]:
    targets = ensure_ask_me_stage_targets(
        db,
        series_id="series_capability",
        capability_revision_id=capability_revision_id,
        concept_revision_id="revision_recursion",
    )
    contract = LearningContractVersion(
        id="contract_capability",
        section_id="section_capability",
        mission_version_id="mission_capability",
        version=1,
        section_question_snapshot="递归如何工作？",
        target_depth="deep",
        boundaries_json="[]",
        generation_context_json="{}",
        provenance_mode="route_scoped_knowledge",
        lineage_status="verified",
        contract_hash="c" * 64,
    )
    db.add(contract)
    for position, (dimension, policy) in enumerate(
        (
            ("mechanism", "oral_explanation_v1"),
            ("boundary", "oral_boundary_v1"),
            ("transfer", "oral_transfer_probe_v1"),
            ("application", "standard_application_v1"),
        ),
        1,
    ):
        db.add(
            LearningContractAssessmentTarget(
                id=f"contract_target_{dimension}",
                contract_version_id=contract.id,
                assessment_target_id=targets[dimension].id,
                position=position,
                required=False,
                verification_policy=policy,
                evidence_policy="capability_evidence_v1",
                diagnostic_only=True,
            )
        )
    db.flush()
    return contract, targets


def test_two_strong_oral_criteria_promote_silver_but_transfer_probe_cannot_promote() -> None:
    with _session() as db:
        _add_concept(db)
        capability, bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        _add_target(
            db,
            target_id="target_bronze_oral_path",
            capability_revision_id=capability.id,
            criterion_id=bronze.id,
            dimension="recognition",
        )
        _add_observation(
            db,
            suffix="bronze_oral_path",
            target_id="target_bronze_oral_path",
        )
        contract, targets = _add_ask_me_contract(
            db,
            capability_revision_id=capability.id,
        )
        db.flush()
        rebuild_assessment_projections(db, user_id="user_capability")

        record_ask_me_assessment_facts(
            db,
            learning_run_id="run_capability",
            user_id="user_capability",
            section_id="section_capability",
            learning_contract_version_id=contract.id,
            content_version_id=None,
            assessment_target_ids=[targets["mechanism"].id],
            source_type="ask_me_topic",
            source_id="topic_mechanism",
            evaluation="strong",
            dimension="mechanism",
            payload={},
        )
        state = db.scalar(select(CapabilityStateProjection))
        assert state.current_stage == "bronze"
        assert len(json.loads(state.missing_criterion_ids_json)) == 1

        record_ask_me_assessment_facts(
            db,
            learning_run_id="run_capability",
            user_id="user_capability",
            section_id="section_capability",
            learning_contract_version_id=contract.id,
            content_version_id=None,
            assessment_target_ids=[targets["boundary"].id],
            source_type="ask_me_topic",
            source_id="topic_boundary",
            evaluation="strong",
            dimension="boundary",
            payload={},
        )
        db.refresh(state)
        assert state.current_stage == "silver"
        assert state.current_stage_order == 2

        transfer_rows = record_ask_me_assessment_facts(
            db,
            learning_run_id="run_capability",
            user_id="user_capability",
            section_id="section_capability",
            learning_contract_version_id=contract.id,
            content_version_id=None,
            assessment_target_ids=[targets["transfer"].id],
            source_type="ask_me_topic",
            source_id="topic_transfer",
            evaluation="strong",
            dimension="transfer",
            payload={},
        )
        transfer_qualification = db.scalar(
            select(EvidenceQualificationEvent).where(
                EvidenceQualificationEvent.observation_id == transfer_rows[0].id,
                EvidenceQualificationEvent.projection_family == "capability",
            )
        )
        db.refresh(state)
        assert transfer_qualification.status == "ineligible"
        assert state.current_stage == "silver"


def test_oral_protocol_mismatch_fails_closed() -> None:
    with _session() as db:
        _add_concept(db)
        capability, _bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        contract, targets = _add_ask_me_contract(
            db,
            capability_revision_id=capability.id,
        )

        with pytest.raises(AppError) as raised:
            record_ask_me_assessment_facts(
                db,
                learning_run_id="run_capability",
                user_id="user_capability",
                section_id="section_capability",
                learning_contract_version_id=contract.id,
                content_version_id=None,
                assessment_target_ids=[targets["mechanism"].id],
                source_type="ask_me_topic",
                source_id="topic_mismatched",
                evaluation="strong",
                dimension="boundary",
                payload={},
            )
        assert raised.value.code == "ASK_ME_CAPABILITY_PROTOCOL_INVALID"


@pytest.mark.parametrize("evaluation", ["partial", "weak"])
def test_non_strong_oral_results_do_not_satisfy_silver(evaluation: str) -> None:
    with _session() as db:
        _add_concept(db)
        capability, bronze = ensure_route_capability(
            db,
            series_id="series_capability",
            concept_revision_id="revision_recursion",
        )
        _add_target(
            db,
            target_id="target_bronze_non_strong",
            capability_revision_id=capability.id,
            criterion_id=bronze.id,
            dimension="recognition",
        )
        _add_observation(
            db,
            suffix="bronze_non_strong",
            target_id="target_bronze_non_strong",
        )
        contract, targets = _add_ask_me_contract(
            db,
            capability_revision_id=capability.id,
        )
        rebuild_assessment_projections(db, user_id="user_capability")

        for dimension in ("mechanism", "boundary"):
            record_ask_me_assessment_facts(
                db,
                learning_run_id="run_capability",
                user_id="user_capability",
                section_id="section_capability",
                learning_contract_version_id=contract.id,
                content_version_id=None,
                assessment_target_ids=[targets[dimension].id],
                source_type="ask_me_topic",
                source_id=f"topic_{dimension}_{evaluation}",
                evaluation=evaluation,
                dimension=dimension,
                payload={},
            )

        state = db.scalar(select(CapabilityStateProjection))
        assert state.current_stage == "bronze"
        assert len(json.loads(state.missing_criterion_ids_json)) == 2


class _ApplicationAi:
    configured = True
    model = "author-model"

    def __init__(self, *, same_family: bool = False, incomplete_rubric: bool = False):
        self.same_family = same_family
        self.incomplete_rubric = incomplete_rubric
        self.last_deployment_id = ""
        self.last_model_family_id = ""

    async def author_standard_application_task(self, request):
        self.last_deployment_id = "application-author"
        self.last_model_family_id = "author-family"
        self.model = "author-model"
        return StandardApplicationTaskCandidate(
            prompt=(
                "某服务把一个大问题拆成规模更小的同类问题处理。"
                "请设计终止条件和处理步骤，并给出可观察验证信号与失败边界。"
            ),
            task_context="一个正文未直接出现过的标准服务排障案例",
            deliverables=["判断", "步骤", "验证信号", "失败边界"],
            rubric=[
                StandardApplicationRubricCriterion(
                    criterion_key="C1",
                    statement="正确运用递归的缩小问题与终止机制",
                ),
                StandardApplicationRubricCriterion(
                    criterion_key="C2",
                    statement="给出可执行步骤、验证信号和失败边界",
                ),
            ],
            reference_answer_points=[
                "每次调用都必须缩小问题规模",
                "基本情形必须覆盖并可被验证",
            ],
            novelty_basis="使用服务排障实例，不复用正文中的树遍历示例",
        )

    async def evaluate_standard_application_submission(self, request):
        self.last_deployment_id = "application-evaluator"
        self.last_model_family_id = (
            "author-family" if self.same_family else "evaluator-family"
        )
        self.model = "evaluator-model"
        rubric = request["rubric"]
        if self.incomplete_rubric:
            rubric = [rubric[0], {**rubric[1], "criterionKey": "C3"}]
        return StandardApplicationEvaluation(
            verdict="pass",
            evidence_sufficiency="sufficient",
            criterion_results=[
                StandardApplicationCriterionResult(
                    criterion_key=item["criterionKey"],
                    satisfied=True,
                    rationale="提交明确覆盖了该项冻结标准。",
                )
                for item in rubric
            ],
            rationale="提交满足全部必需标准。",
        )


class _NonNovelApplicationAi(_ApplicationAi):
    async def author_standard_application_task(self, request):
        candidate = await super().author_standard_application_task(request)
        return candidate.model_copy(
            update={"prompt": request["publishedContentBlocks"][0]["content"]}
        )


def _application_service_fixture(
    db: Session,
    *,
    ai=None,
    silver: bool = True,
) -> tuple[CapabilityApplicationTaskService, str]:
    _add_concept(db)
    capability, bronze = ensure_route_capability(
        db,
        series_id="series_capability",
        concept_revision_id="revision_recursion",
    )
    _add_target(
        db,
        target_id="target_bronze_application_path",
        capability_revision_id=capability.id,
        criterion_id=bronze.id,
        dimension="recognition",
    )
    _add_observation(
        db,
        suffix="bronze_application_path",
        target_id="target_bronze_application_path",
    )
    contract, targets = _add_ask_me_contract(
        db,
        capability_revision_id=capability.id,
    )
    if silver:
        for dimension in ("mechanism", "boundary"):
            record_ask_me_assessment_facts(
                db,
                learning_run_id="run_capability",
                user_id="user_capability",
                section_id="section_capability",
                learning_contract_version_id=contract.id,
                content_version_id="content_capability",
                assessment_target_ids=[targets[dimension].id],
                source_type="ask_me_topic",
                source_id=f"topic_application_{dimension}",
                evaluation="strong",
                dimension=dimension,
                payload={},
            )
    else:
        rebuild_assessment_projections(db, user_id="user_capability")
    content = ContentVersion(
        id="content_capability",
        section_id="section_capability",
        learning_contract_version_id=contract.id,
        version=1,
        blocks_json="[]",
        sources_json="[]",
        confidence="high",
        publication_status="published",
    )
    db.add(content)
    db.add(
        ContentBlockVersion(
            id="block_capability_core",
            content_version_id=content.id,
            position=1,
            format_kind="markdown",
            semantic_role="core_instruction",
            heading="递归的终止机制",
            content=(
                "递归必须让每一次调用都缩小问题规模，并由明确的基本情形停止。"
                "正文示例使用树遍历说明调用过程。"
            ),
            assessment_eligible=True,
        )
    )
    db.add(
        LearningRunSectionBinding(
            id="run_section_capability",
            learning_run_id="run_capability",
            user_id="user_capability",
            section_id="section_capability",
            learning_contract_version_id=contract.id,
            content_version_id=content.id,
            initial_quiz_set_id=None,
            first_read_at=now(),
            source="test",
        )
    )
    db.flush()

    class Contexts:
        def resolve_section(self, *, user_id, section_id):
            assert user_id == "user_capability"
            assert section_id == "section_capability"
            return SimpleNamespace(
                series=SimpleNamespace(id="series_capability"),
                section=SimpleNamespace(id=section_id),
            )

    class Progress:
        def active_run(self, series_id):
            assert series_id == "series_capability"
            return SimpleNamespace(id="run_capability")

    return (
        CapabilityApplicationTaskService(
            db,
            user_id="user_capability",
            ai=ai or _ApplicationAi(),
            contexts=Contexts(),
            progress=Progress(),
        ),
        capability.id,
    )


def test_unseen_unassisted_independently_evaluated_task_promotes_gold() -> None:
    with _session() as db:
        service, capability_id = _application_service_fixture(db)
        task = asyncio.run(service.prepare("section_capability"))
        result = asyncio.run(service.submit(
            task["id"],
            response={
                "judgment": "每次处理都缩小问题，并在空输入时终止",
                "steps": ["检查基本情形", "拆分子问题", "组合返回值"],
                "validation": "记录每层输入规模，确认严格递减并最终为零",
                "boundary": "无法证明规模递减时可能无限调用",
            },
            assistance_used=False,
            idempotency_key="gold-success-001",
        ))

        state = db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.capability_revision_id == capability_id
            )
        )
        task_row = db.get(CapabilityApplicationTaskVersion, task["id"])
        evaluation = db.scalar(select(CapabilityApplicationEvaluation))

        assert task["evidenceEligible"] is True
        assert task_row.publication_status == "published"
        assert result["evidenceEligible"] is True
        assert result["capabilityStage"] == "gold"
        assert state.current_stage == "gold"
        assert evaluation.qualification_status == "eligible"


@pytest.mark.parametrize(
    ("ai", "assistance_used", "reason"),
    [
        (_ApplicationAi(same_family=True), False, "evaluation_not_independent"),
        (_ApplicationAi(), True, "declared_assistance_used"),
    ],
)
def test_non_independent_or_assisted_application_cannot_promote_gold(
    ai, assistance_used: bool, reason: str
) -> None:
    with _session() as db:
        service, capability_id = _application_service_fixture(db, ai=ai)
        task = asyncio.run(service.prepare("section_capability"))
        result = asyncio.run(service.submit(
            task["id"],
            response={"answer": "完整但不具备正式资格的应用回答" * 8},
            assistance_used=assistance_used,
            idempotency_key=f"gold-ineligible-{reason}",
        ))
        evaluation = db.scalar(select(CapabilityApplicationEvaluation))
        state = db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.capability_revision_id == capability_id
            )
        )

        assert result["evidenceEligible"] is False
        assert evaluation.qualification_reason == reason
        assert state.current_stage == "silver"


def test_application_submission_requires_cumulative_silver_stage() -> None:
    with _session() as db:
        service, _capability_id = _application_service_fixture(db, silver=False)
        task = asyncio.run(service.prepare("section_capability"))

        with pytest.raises(AppError) as raised:
            asyncio.run(service.submit(
                task["id"],
                response={"answer": "尝试越过白银直接完成黄金"},
                assistance_used=False,
                idempotency_key="gold-before-silver",
            ))

        assert raised.value.code == "APPLICATION_TASK_SILVER_REQUIRED"
        assert db.scalar(select(CapabilityApplicationSubmission)) is None


def test_application_task_rejects_seen_content_copy() -> None:
    with _session() as db:
        service, _capability_id = _application_service_fixture(
            db, ai=_NonNovelApplicationAi()
        )

        with pytest.raises(AppError) as raised:
            asyncio.run(service.prepare("section_capability"))

        assert raised.value.code == "APPLICATION_TASK_NOT_NOVEL"
        assert db.scalar(select(CapabilityApplicationTaskVersion)) is None


def test_application_evaluation_requires_complete_frozen_rubric() -> None:
    with _session() as db:
        service, _capability_id = _application_service_fixture(
            db, ai=_ApplicationAi(incomplete_rubric=True)
        )
        task = asyncio.run(service.prepare("section_capability"))

        with pytest.raises(AppError) as raised:
            asyncio.run(service.submit(
                task["id"],
                response={"answer": "覆盖部分标准的回答" * 8},
                assistance_used=False,
                idempotency_key="gold-rubric-missing",
            ))

        assert raised.value.code == "APPLICATION_EVALUATION_RUBRIC_COVERAGE_INVALID"
        assert db.scalar(select(CapabilityApplicationEvaluation)) is None
