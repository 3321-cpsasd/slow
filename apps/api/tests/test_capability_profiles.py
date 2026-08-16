import asyncio
import json
from datetime import timedelta
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
    CapabilityReviewTaskVersion,
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
    ReviewAssignment,
    SectionAssessmentTarget,
    now,
)
from app.ai.contracts import (
    CapabilityReviewRubricCriterion,
    CapabilityReviewTaskCandidate,
    StandardApplicationCriterionResult,
    StandardApplicationEvaluation,
    StandardApplicationRubricCriterion,
    StandardApplicationTaskCandidate,
    TransferTaskCandidate,
)
from app.modules.learning.assessment import (
    QUALIFICATION_RULE_VERSION,
    record_ask_me_assessment_facts,
    rebuild_assessment_projections,
)
from app.modules.learning.capabilities import (
    CapabilityConceptSpec,
    CapabilityRelationSpec,
    ensure_ask_me_stage_targets,
    ensure_route_capability,
    ensure_route_capability_subnet,
)
from app.modules.learning.application_tasks import CapabilityApplicationTaskService
from app.modules.learning.review_stage_tasks import CapabilityReviewTaskService


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
            ("transfer_task", "transfer_task_v1"),
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

    async def author_transfer_task(self, request):
        self.last_deployment_id = "transfer-author"
        self.last_model_family_id = "author-family"
        self.model = "author-model"
        labels = [item["label"] for item in request["requiredKnowledge"]][:2]
        return TransferTaskCandidate(
            prompt=(
                f"一个陌生的分布式作业同时出现终止判断不可靠和子任务重复执行。"
                f"请重组{labels[0]}与{labels[1]}设计处理方案，并解释选择理由、验证信号和失效边界。"
            ),
            task_context="分布式作业在网络分区和重试并存时的综合故障",
            deliverables=["综合方案", "知识重组", "选择理由", "验证信号", "失效边界"],
            rubric=[
                StandardApplicationRubricCriterion(
                    criterion_key="C1", statement="正确重组两项必需知识"
                ),
                StandardApplicationRubricCriterion(
                    criterion_key="C2", statement="解释方案选择及关系机制"
                ),
                StandardApplicationRubricCriterion(
                    criterion_key="C3", statement="提供验证信号和失效边界"
                ),
            ],
            reference_answer_points=["两项知识共同参与决策", "理由与验证边界对应"],
            novelty_basis="分布式网络分区与重试组合未出现在正文示例中",
            unfamiliarity_basis="需要适配正文未教授的分布式约束并综合决策",
            required_knowledge_recombination=labels,
            decision_rationale_requirement="解释为何必须组合两项知识而非独立套用",
        )

    async def evaluate_transfer_submission(self, request):
        return await self.evaluate_standard_application_submission(request)

    async def author_capability_review_task(self, request):
        self.last_deployment_id = "capability-review-author"
        self.last_model_family_id = "author-family"
        self.model = "author-model"
        labels = [item["label"] for item in request.get("requiredKnowledge", [])]
        task_kind = request["taskKind"]
        rubric_sources = list(request["plannedCriteria"])
        if len(rubric_sources) == 1:
            rubric_sources.append(request["plannedCriteria"][0])
        return CapabilityReviewTaskCandidate(
            prompt=(
                "请在一个正文未出现的新运行情境中重新完成当前阶段任务，"
                "说明判断、操作理由、验证信号以及方案失效的边界。"
            ),
            task_context="延迟复习生成的新运行情境",
            deliverables=["判断", "理由", "验证", "边界"],
            rubric=[
                CapabilityReviewRubricCriterion(
                    criterion_key=f"C{index}",
                    stage_criterion_id=item["id"],
                    statement=item["statement"],
                )
                for index, item in enumerate(rubric_sources, 1)
            ],
            reference_answer_points=[
                item["statement"] for item in request["plannedCriteria"]
            ],
            novelty_basis="新运行情境不复用正文案例。",
            unfamiliarity_basis=(
                "陌生约束要求重新组合能力子网。"
                if task_kind == "transfer_reactivation"
                else ""
            ),
            required_knowledge_recombination=(
                labels[:2] if task_kind == "transfer_reactivation" else []
            ),
        )

    async def evaluate_capability_review_submission(self, request):
        return await self.evaluate_standard_application_submission(request)


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
    diamond_route: bool = False,
) -> tuple[CapabilityApplicationTaskService, str]:
    _add_concept(db)
    if diamond_route:
        db.add(
            Concept(id="concept_idempotency", namespace="test", concept_key="idempotency", canonical_name="幂等性", status="active", origin="test")
        )
        db.add(
            ConceptRevision(id="revision_idempotency", concept_id="concept_idempotency", revision=1, label="幂等性", definition="重复执行保持相同效果。", scope_json="{}", verification_status="route_scoped")
        )
        capability, bronze = ensure_route_capability_subnet(
            db,
            series_id="series_capability",
            label="在复杂执行环境中综合运用递归与幂等性",
            concepts=(
                CapabilityConceptSpec(concept_revision_id="revision_recursion", role="anchor", required=True),
                CapabilityConceptSpec(concept_revision_id="revision_idempotency", role="required", required=True),
            ),
            relations=(
                CapabilityRelationSpec(
                    "revision_recursion",
                    "revision_idempotency",
                    "enables",
                    "递归控制分解与终止，幂等性控制重复执行的副作用。",
                    minimum_stage="silver",
                    purpose="integrated_application",
                ),
            ),
            natural_stage_ceiling="diamond",
        )
    else:
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


def test_unfamiliar_recombination_task_promotes_gold_to_diamond() -> None:
    with _session() as db:
        service, capability_id = _application_service_fixture(
            db, diamond_route=True
        )
        gold = asyncio.run(service.prepare("section_capability"))
        asyncio.run(service.submit(
            gold["id"],
            response={"answer": "标准任务中给出终止、步骤、验证和边界" * 5},
            assistance_used=False,
            idempotency_key="diamond-gold-first",
        ))
        transfer = asyncio.run(
            service.prepare("section_capability", "transfer_task")
        )
        result = asyncio.run(service.submit(
            transfer["id"],
            response={
                "plan": "用规模递减保证终止，用幂等键吸收重试，两者共同约束执行",
                "rationale": "只保证终止不能避免重试副作用，只保证幂等不能阻止无限分解",
                "validation": "观察任务规模单调下降且同一幂等键只产生一次效果",
                "boundary": "不可生成稳定幂等键或规模不可度量时方案失效",
            },
            assistance_used=False,
            idempotency_key="diamond-success-001",
            expected_task_kind="transfer_task",
        ))

        state = db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.capability_revision_id == capability_id
            )
        )
        task_row = db.get(CapabilityApplicationTaskVersion, transfer["id"])
        route = db.scalar(
            select(CapabilityRouteBinding).where(
                CapabilityRouteBinding.capability_revision_id == capability_id
            )
        )
        assert transfer["taskKind"] == "transfer_task"
        assert len(transfer["requiredKnowledgeRecombination"]) == 2
        assert task_row.context_fingerprint
        assert result["evidenceEligible"] is True
        assert result["capabilityStage"] == "diamond"
        assert state.current_stage == "diamond"
        assert route.target_stage == "diamond"


def test_transfer_submission_cannot_skip_gold() -> None:
    with _session() as db:
        service, capability_id = _application_service_fixture(
            db, diamond_route=True
        )
        asyncio.run(service.prepare("section_capability"))
        transfer = asyncio.run(
            service.prepare("section_capability", "transfer_task")
        )

        with pytest.raises(AppError) as raised:
            asyncio.run(service.submit(
                transfer["id"],
                response={"answer": "试图从白银直接完成迁移" * 8},
                assistance_used=False,
                idempotency_key="diamond-before-gold",
                expected_task_kind="transfer_task",
            ))

        state = db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.capability_revision_id == capability_id
            )
        )
        assert raised.value.code == "TRANSFER_TASK_GOLD_REQUIRED"
        assert state.current_stage == "silver"


@pytest.mark.parametrize(
    ("stage", "task_kind", "assistance_used", "expected_qualified"),
    [
        ("gold", "application_reactivation", False, True),
        ("gold", "application_reactivation", True, False),
        ("diamond", "transfer_reactivation", False, True),
    ],
)
def test_high_stage_review_reactivates_without_regranting_stage(
    stage: str,
    task_kind: str,
    assistance_used: bool,
    expected_qualified: bool,
) -> None:
    with _session() as db:
        ai = _ApplicationAi()
        application_service, capability_id = _application_service_fixture(
            db,
            ai=ai,
            diamond_route=True,
        )
        gold_task = asyncio.run(application_service.prepare("section_capability"))
        asyncio.run(application_service.submit(
            gold_task["id"],
            response={"answer": "标准应用回答包含判断、步骤、验证和边界" * 4},
            assistance_used=False,
            idempotency_key=f"{stage}-review-gold-prerequisite",
        ))
        if stage == "diamond":
            transfer_task = asyncio.run(
                application_service.prepare("section_capability", "transfer_task")
            )
            asyncio.run(application_service.submit(
                transfer_task["id"],
                response={"answer": "陌生情境中重组递归、幂等性并说明选择依据" * 4},
                assistance_used=False,
                idempotency_key="diamond-review-prerequisite",
                expected_task_kind="transfer_task",
            ))
        criterion_ids = list(
            db.scalars(
                select(CapabilityStageCriterion.id).where(
                    CapabilityStageCriterion.capability_revision_id == capability_id,
                    CapabilityStageCriterion.stage == stage,
                )
            )
        )
        moment = now()
        assignment = ReviewAssignment(
            id=f"review_assignment_{stage}",
            selection_run_id=f"selection_{stage}",
            review_state_id=f"review_state_{stage}",
            user_id="user_capability",
            assessment_target_id="target_bronze_application_path",
            source_learning_run_id="run_capability",
            source_section_id="section_capability",
            learning_contract_version_id="contract_capability",
            content_version_id="content_capability",
            prior_quiz_set_id=f"prior_quiz_{stage}",
            due_at=moment - timedelta(days=1),
            expires_at=moment + timedelta(days=1),
            status="started",
            rank=1,
            base_priority=40,
            effective_priority=40,
            selection_rule_version="review_assignment_v2_capability_priority",
            qualification_rule_version=QUALIFICATION_RULE_VERSION,
            task_plan_json=json.dumps({
                "ruleVersion": "review_task_plan_v1",
                "reactivation": {
                    "purpose": "retention_reactivation",
                    "taskKind": task_kind,
                    "stage": stage,
                    "criterionIds": criterion_ids,
                    "verificationProtocols": [],
                    "evidenceEffect": "activation_only",
                },
                "strengthening": None,
            }),
            task_plan_rule_version="review_task_plan_v1",
            prior_item_signatures_json="[]",
            item_signatures_json="[]",
            last_event_at=moment,
        )
        db.add(assignment)
        db.flush()
        review_service = CapabilityReviewTaskService(
            db,
            user_id="user_capability",
            ai=ai,
        )
        task = asyncio.run(review_service.prepare(assignment))
        result, _submission = asyncio.run(review_service.submit(
            assignment,
            response={"answer": "重新展示当前阶段能力并说明判断依据与失效边界" * 4},
            assistance_used=assistance_used,
            idempotency_key=f"{stage}-reactivation-submit",
        ))

        state = db.scalar(
            select(CapabilityStateProjection).where(
                CapabilityStateProjection.capability_revision_id == capability_id
            )
        )
        capability_qualification = db.scalar(
            select(EvidenceQualificationEvent)
            .join(
                AssessmentObservation,
                AssessmentObservation.id
                == EvidenceQualificationEvent.observation_id,
            )
            .where(
                AssessmentObservation.source_type == "capability_review",
                EvidenceQualificationEvent.projection_family == "capability",
            )
        )
        assert task.task_kind == task_kind
        assert result["reactivationQualified"] is expected_qualified
        assert result["stageChanged"] is False
        assert state.current_stage == stage
        assert capability_qualification.status == "ineligible"
        if stage == "diamond":
            assert len(json.loads(task.required_knowledge_json)) == 2


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
