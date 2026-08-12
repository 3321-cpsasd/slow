import itertools
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai.contracts import (
    GeneratedLessonBlock,
    GeneratedLessonCandidate,
    GeneratedLessonFeedbackReplacement,
    GeneratedLessonQuestion,
)
from app.application import section_generation
from app.application.service import SlowService
from app.application.lesson_generation import (
    CandidateValidationFailure,
    LessonGenerationSpec,
    publish_lesson_candidate,
    validate_lesson_candidate,
)
from app.application.standard_content import StandardContentService
from app.infrastructure.tables import (
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    Base,
    ContentBlockAssessmentTarget,
    ContentVersion,
    GenerationRun,
    LearningContractVersion,
    QuizSet,
    Section,
    SectionFallbackBinding,
    StandardLessonPackageVersion,
)
from app.main import create_app
from app.core.errors import AppError
from app.services.source_verifier import AcceptingSourceVerifier
from test_vertical_slice import FakeAi, create_series, sse_events, wait_for_task


def spec(*, second_required=False, feedback=None):
    return LessonGenerationSpec.model_validate(
        {
            "generationMode": "model_only",
            "mission": {"versionId": "mission_1", "why": "理解机制"},
            "learner": {"profession": "工程师"},
            "section": {
                "id": "section_1",
                "title": "一次只学一个锚点",
                "question": "为什么需要稳定绑定？",
            },
            "learningContractVersionId": "contract_1",
            "learningContractVersion": 3,
            "targets": [
                {
                    "assessmentTargetId": "target_core",
                    "objective": "解释稳定绑定的作用",
                    "dimension": "mechanism",
                    "targetDepth": "deep",
                    "required": True,
                    "verificationPolicy": "choice_quiz_v1",
                },
                {
                    "assessmentTargetId": "target_boundary",
                    "objective": "识别绑定失效边界",
                    "dimension": "boundary",
                    "targetDepth": "deep",
                    "required": second_required,
                    "verificationPolicy": "choice_quiz_v1",
                },
            ],
            "neighborBoundaries": [],
            "relevantMastery": [],
            "depthPolicy": {"scope": "机制与边界"},
            "feedback": feedback or {},
        }
    )


def candidate():
    roles = [
        ("core_instruction", "core", ["target_core"]),
        ("core_instruction", "core", ["target_boundary"]),
        ("comparison", "comparison", []),
        ("boundary", "boundary", []),
        ("practice", "practice", []),
    ]
    blocks = [
        GeneratedLessonBlock(
            block_key=f"b{index}",
            kind="text",
            role=role,
            relation_to_anchor=relation,
            assessment_target_ids=target_ids,
            heading=f"块 {index}",
            content="这一正文块完整解释当前目标的机制、判断依据与适用边界，并为绑定题目提供直接证据。",
        )
        for index, (role, relation, target_ids) in enumerate(roles, 1)
    ]
    questions = [
        GeneratedLessonQuestion(
            item_key=f"q{index}",
            assessment_target_id="target_core",
            evidence_block_keys=["b1"],
            prompt=f"第 {index} 题：哪项符合正文讲授的稳定绑定机制？",
            options=["仅匹配标题", "使用稳定目标 ID", "忽略契约"],
            correct=[1],
            explanation="正文明确要求使用稳定目标 ID。",
        )
        for index in range(1, 5)
    ]
    return GeneratedLessonCandidate(blocks=blocks, questions=questions)


def test_valid_candidate_passes_without_repair():
    validated = validate_lesson_candidate(spec(), candidate())
    assert validated.block_by_key["b1"].assessment_target_ids == ["target_core"]


@pytest.mark.parametrize(
    "explanation",
    [
        "选项3需要或语义，因此无法表达。",
        "第 3 个选项需要或语义，因此无法表达。",
        "C 项需要或语义，因此无法表达。",
        "Option C requires OR semantics.",
    ],
)
def test_option_position_dependent_explanation_is_rejected(explanation):
    value = candidate()
    value.questions[0].explanation = explanation

    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)

    assert raised.value.code == "ASSESSMENT_EXPLANATION_POSITION_DEPENDENT"
    assert raised.value.location == {
        "itemKey": "q1",
        "rule": "positional_option_reference",
        "schemaVersion": "generated_lesson_composition_candidate_v7",
    }


def test_single_answer_explanation_cannot_hedge_multiple_valid_options():
    value = candidate()
    value.questions[0].explanation = (
        "实际上另外两种需求也无法表达，但这是最佳答案，因为它更典型。"
    )

    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)

    assert raised.value.code == "ASSESSMENT_SINGLE_ANSWER_AMBIGUOUS"
    assert raised.value.location["rule"] == "hedged_single_answer"


def test_explanation_can_name_option_content_after_reordering():
    value = candidate()
    value.questions[0].explanation = (
        "“使用稳定目标 ID”满足正文要求；只匹配标题或忽略契约都不成立。"
    )

    validate_lesson_candidate(spec(), value)


def test_long_text_requires_authored_paragraph_breaks():
    value = candidate()
    value.blocks[0].content = "这是一个需要逐层解释的较长机制段落。" * 18

    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)

    assert raised.value.code == "CONTENT_BLOCK_LAYOUT_INVALID"
    assert raised.value.location == {
        "blockKey": "b1",
        "kind": "text",
        "rule": "long_single_paragraph",
        "schemaVersion": "generated_lesson_composition_candidate_v7",
        "characterCount": len(value.blocks[0].content),
        "paragraphCount": 1,
    }

    value.blocks[0].content = (
        "这是第一段，用来说明问题、对象、约束和可观察结果。" * 6
        + "\n\n"
        + "这是第二段，用来继续解释机制、判断依据和适用边界。" * 6
    )
    validate_lesson_candidate(spec(), value)


def test_standard_blocks_accept_mixed_gfm_regardless_of_presentation_hint():
    markdown_samples = {
        "text": (
            "下面是判断依据：\n\n"
            "- 第一项依据说明。\n"
            "- 第二项依据说明。\n\n"
            "1. 先观察条件。\n"
            "2. 再判断结果。"
        ),
        "bullet_list": "这一块以连贯段落解释机制；展示提示不应成为内容格式门禁。",
        "ordered_steps": (
            "步骤之间也可以补充并列条件：\n\n"
            "- 条件一必须成立。\n"
            "- 条件二必须成立。"
        ),
        "table": (
            "先用一句话建立比较背景。\n\n"
            "| 环节 | 主要作用 |\n"
            "| --- | --- |\n"
            "| 应用 | 组织用户任务 |\n"
            "| 模型 | 提供推理能力 |\n\n"
            "表格之后可以继续解释结论。"
        ),
        "code": (
            "下面先说明这段程序的目的。\n\n"
            "```python\n"
            "result = bind(target_id='stable')\n"
            "```\n\n"
            "代码之后的文字仍然属于普通正文。"
        ),
    }

    for kind, content in markdown_samples.items():
        value = candidate()
        value.blocks[0].kind = kind
        value.blocks[0].content = content
        validate_lesson_candidate(spec(), value)


def test_unclosed_markdown_code_fence_is_rejected_before_publication():
    value = candidate()
    value.blocks[0].kind = "code"
    value.blocks[0].content = (
        "下面先说明这段程序的目的。\n\n"
        "```python\n"
        "result = bind(target_id='stable')\n\n"
        "这句话原本应该显示在代码块外。"
    )

    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)

    assert raised.value.code == "CONTENT_BLOCK_LAYOUT_INVALID"
    assert raised.value.location == {
        "blockKey": "b1",
        "kind": "code",
        "rule": "unclosed_code_fence",
        "schemaVersion": "generated_lesson_composition_candidate_v7",
    }


def _grounded_spec():
    value = spec()
    targets = [
        item.model_copy(
            update={
                "concept_revision_id": (
                    "concept_core" if item.assessment_target_id == "target_core"
                    else "concept_boundary"
                )
            }
        )
        for item in value.targets
    ]
    return value.model_copy(
        update={
            "targets": targets,
            "knowledge_context": {
                "status": "ready",
                "contextHash": "knowledge_hash",
                "claims": [
                    {
                        "claimVersionId": "claim_core",
                        "scope": {"conceptRevisionIds": ["concept_core"]},
                    },
                    {
                        "claimVersionId": "claim_boundary",
                        "scope": {"conceptRevisionIds": ["concept_boundary"]},
                    },
                ],
            },
        }
    )


def test_grounded_candidate_requires_explicit_in_scope_claim_ids():
    value = candidate()
    for block in value.blocks:
        block.claim_version_ids = (
            [] if block.role == "practice" else
            ["claim_core"] if block.assessment_target_ids == ["target_core"] else
            ["claim_boundary"]
        )
    validate_lesson_candidate(_grounded_spec(), value)

    missing = candidate()
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(_grounded_spec(), missing)
    assert raised.value.code == "CONTENT_KNOWLEDGE_CLAIM_MISSING"

    mismatched = candidate()
    for block in mismatched.blocks:
        block.claim_version_ids = ["claim_boundary"]
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(_grounded_spec(), mismatched)
    assert raised.value.code == "CONTENT_KNOWLEDGE_CLAIM_SCOPE_MISMATCH"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda value: value.blocks[0].assessment_target_ids.append("outside"),
            "CONTENT_ASSESSMENT_TARGET_UNBOUND",
        ),
        (
            lambda value: setattr(value.questions[0], "assessment_target_id", "outside"),
            "ASSESSMENT_TARGET_UNBOUND",
        ),
        (
            lambda value: setattr(value.questions[0], "evidence_block_keys", ["missing"]),
            "ASSESSMENT_EVIDENCE_BLOCK_UNBOUND",
        ),
        (
            lambda value: setattr(value.questions[0], "evidence_block_keys", ["b3"]),
            "ASSESSMENT_EVIDENCE_TARGET_MISMATCH",
        ),
    ],
)
def test_candidate_gate_fails_closed_with_machine_code(mutation, expected_code):
    value = candidate()
    mutation(value)
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)
    assert raised.value.code == expected_code
    assert raised.value.location


def test_required_target_must_be_taught_and_assessed():
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(second_required=True), candidate())
    assert raised.value.code == "REQUIRED_TARGET_NOT_ASSESSED"


def test_feedback_candidate_requires_an_explicit_stable_block_mapping():
    feedback = {"blockId": "block_original_1"}
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(feedback=feedback), candidate())
    assert raised.value.code == "FEEDBACK_REPLACEMENT_MAPPING_REQUIRED"

    value = candidate()
    value.feedback_replacement = GeneratedLessonFeedbackReplacement(
        source_block_id="block_original_1",
        replacement_block_key="missing",
    )
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(feedback=feedback), value)
    assert raised.value.code == "FEEDBACK_REPLACEMENT_BLOCK_UNBOUND"


def test_replan_candidate_never_enters_publication_gate():
    value = GeneratedLessonCandidate(
        decision="replan_required",
        replan_code="PREREQUISITE_GAP_REQUIRES_REPLAN",
        replan_reason="当前小节无法补足大型前置缺口",
    )
    with pytest.raises(CandidateValidationFailure) as raised:
        validate_lesson_candidate(spec(), value)
    assert raised.value.code == "PREREQUISITE_GAP_REQUIRES_REPLAN"


def test_atomic_publisher_normalizes_explicit_bindings_and_rolls_back():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    ids = itertools.count(1)

    def uid(prefix):
        return f"{prefix}_{next(ids)}"

    with Session(engine) as db:
        section = Section(
            id="section_1",
            chapter_id="chapter_1",
            position=1,
            title="稳定绑定",
            question="为什么需要稳定绑定？",
            objectives_json="[]",
        )
        contract = LearningContractVersion(
            id="contract_1",
            section_id=section.id,
            mission_version_id="mission_1",
            version=3,
            section_question_snapshot=section.question,
            target_depth="deep",
            boundaries_json="[]",
            generation_context_json="{}",
            provenance_mode="native_m2",
            lineage_status="confirmed",
            contract_hash="hash",
        )
        run = GenerationRun(
            id="run_1",
            section_id=section.id,
            operation="lesson",
            attempt=1,
            status="validating",
            model="test-model",
        )
        validated = validate_lesson_candidate(spec(), candidate())
        published = publish_lesson_candidate(
            db,
            uid=uid,
            section=section,
            contract=contract,
            generation_run=run,
            spec=spec(),
            validated=validated,
            content_version=1,
            quiz_generation=1,
        )
        assert published.content.publication_status == "published"
        assert published.quiz.publication_status == "published"
        payload = published.content.blocks_json
        assert '"teachingMoves"' in payload
        assert '"readerPriority":"essential"' in payload
        assert db.scalar(select(func.count()).select_from(ContentBlockAssessmentTarget)) == 2
        assert db.scalar(select(func.count()).select_from(AssessmentItemVersion)) == 4
        assert db.scalar(select(func.count()).select_from(AssessmentItemEvidenceBlock)) == 4
        db.rollback()
        assert db.scalar(select(func.count()).select_from(ContentVersion)) == 0
        assert db.scalar(select(func.count()).select_from(QuizSet)) == 0


def test_standard_package_requires_exact_contract_and_reuses_normal_gate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        section = Section(
            id="section_1", chapter_id="chapter_1", position=1,
            title="稳定绑定", question="为什么需要稳定绑定？", objectives_json="[]",
        )
        contract = LearningContractVersion(
            id="contract_1", section_id=section.id, mission_version_id="mission_1",
            version=3, section_question_snapshot=section.question,
            target_depth="deep", boundaries_json="[]", generation_context_json="{}",
            provenance_mode="native_m2", lineage_status="confirmed", contract_hash="hash",
        )
        db.add_all([section, contract])
        service = StandardContentService(db)
        package = service.publish_package(
            package_key="stable-binding", version=1, title="稳定绑定标准内容",
            spec=spec(), candidate=candidate(), review={"status": "approved", "reviewer": "fixture"},
        )
        binding = service.bind(
            section=section, contract=contract, spec=spec(), package=package
        )
        fallback, loaded_package = service.fallback_candidate(
            contract=contract, spec=spec()
        )
        assert binding.standard_package_version_id == package.id
        assert loaded_package.id == package.id
        assert validate_lesson_candidate(spec(), fallback)

        mismatched = spec()
        mismatched.section["question"] = "另一个问题"
        with pytest.raises(AppError) as raised:
            service.fallback_candidate(contract=contract, spec=mismatched)
        assert raised.value.code == "GUARANTEED_ROUTE_FALLBACK_MISSING"
        assert db.scalar(select(func.count()).select_from(StandardLessonPackageVersion)) == 1
        assert db.scalar(select(func.count()).select_from(SectionFallbackBinding)) == 1


class V2FakeAi(FakeAi):
    def __init__(self, *, invalid_target=False, invalid_layout=False):
        self.lesson_generation_calls = 0
        self.generated_section_ids = []
        self.invalid_target = invalid_target
        self.invalid_layout = invalid_layout

    async def generate_lesson(self, lesson_spec):
        self.lesson_generation_calls += 1
        self.generated_section_ids.append(lesson_spec["section"]["id"])
        targets = lesson_spec["targets"]
        blocks = []
        roles = [
            *[("core_instruction", "core", target["assessmentTargetId"]) for target in targets],
            ("comparison", "comparison", ""),
            ("boundary", "boundary", ""),
            ("practice", "practice", ""),
        ]
        for index, (role, relation, target_id) in enumerate(roles, 1):
            blocks.append(
                GeneratedLessonBlock(
                    block_key=f"b{index}",
                    kind="text",
                    role=role,
                    relation_to_anchor=relation,
                    assessment_target_ids=[
                        "outside_contract" if self.invalid_target and index == 1 else target_id
                    ] if target_id else [],
                    heading=f"正文块 {index}",
                    content=(
                        "这是一个没有任何分段的较长正文块。" * 20
                        if self.invalid_layout and index == 1
                        else "这一正文块完整解释当前目标的机制、判断依据与适用边界，并为绑定题目提供直接证据。"
                    ),
                )
            )
        questions = []
        for index in range(4):
            target_index = index % len(targets)
            target = targets[target_index]
            block = blocks[target_index]
            questions.append(
                GeneratedLessonQuestion(
                    item_key=f"q{index + 1}",
                    assessment_target_id=target["assessmentTargetId"],
                    evidence_block_keys=[block.block_key],
                    prompt=f"第 {index + 1} 题：哪项符合正文教授的机制？",
                    options=["只看标题", "依据机制判断", "忽略边界"],
                    correct=[1],
                    explanation="正文要求依据机制和边界判断。",
                )
            )
        feedback = lesson_spec.get("feedback") or {}
        return GeneratedLessonCandidate(
            blocks=blocks,
            questions=questions,
            feedback_replacement=(
                GeneratedLessonFeedbackReplacement(
                    source_block_id=feedback["blockId"],
                    # Deliberately map the first old block to the last new block
                    # so the integration test proves identity is not positional.
                    replacement_block_key=blocks[-1].block_key,
                )
                if feedback
                else None
            ),
        )


class V2FailingHarnessAi(V2FakeAi):
    async def generate_lesson(self, lesson_spec):
        raise RuntimeError("simulated v2 provider failure")

    def structured_trace(self):
        return [
            {
                "schema": "GeneratedLessonSlotCandidate",
                "attempts": 1,
                "repairAttempts": 0,
                "outcome": "provider_failed",
                "invalidOutputDigests": [],
                "lastValidationIssues": [],
                "tokenBudgets": [12_000],
            }
        ]


def test_default_v2_route_uses_one_model_call_and_publishes_both_artifacts():
    ai = V2FakeAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        task = wait_for_task(client, series["initializationTask"]["taskId"])
        refreshed = client.get(f"/api/series/{series['id']}").json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
        lesson = client.get(f"/api/sections/{section_id}").json()

        assert task["status"] == "succeeded"
        assert ai.generated_section_ids.count(section_id) == 1
        assert lesson["content"]["publicationStatus"] == "published"
        assert lesson["content"]["boundaryValidation"]["status"] == "passed"
        assert lesson["quiz"]["publicationStatus"] == "published"
        assert lesson["generation"]["trace"]["physicalCallBudget"] == 1
        with client.app.state.sessions() as db:
            assert db.scalar(
                select(func.count()).select_from(AssessmentItemVersion).where(
                    AssessmentItemVersion.quiz_set_id == lesson["quiz"]["id"],
                )
            ) == 4


def test_v2_failure_persists_safe_structured_harness_audit():
    ai = V2FailingHarnessAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        ),
        raise_server_exceptions=False,
    ) as client:
        series = create_series(client)
        task = wait_for_task(client, series["initializationTask"]["taskId"])
        with client.app.state.sessions() as db:
            run = db.scalar(
                select(GenerationRun).order_by(GenerationRun.started_at.desc())
            )
            trace = json.loads(run.trace_json)

            assert task["status"] == "failed"
            assert run.status == "failed"
            assert trace["stage"] == "failed"
            assert trace["aiHarness"] == ai.structured_trace()
            assert "simulated v2 provider failure" not in run.trace_json


def test_v2_route_rejects_unbound_content_before_formal_persistence():
    ai = V2FakeAi(invalid_target=True)
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        task = wait_for_task(client, series["initializationTask"]["taskId"])
        with client.app.state.sessions() as db:
            run = db.scalar(select(GenerationRun).order_by(GenerationRun.started_at.desc()))
            assert task["status"] == "failed"
            assert run.error_code == "CONTENT_ASSESSMENT_TARGET_UNBOUND"
            assert db.scalar(select(func.count()).select_from(ContentVersion)) == 0
            assert db.scalar(select(func.count()).select_from(QuizSet)) == 0


def test_v2_candidate_gate_failure_is_exposed_as_retryable():
    ai = V2FakeAi(invalid_target=True)
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        ),
        raise_server_exceptions=False,
    ) as client:
        series = create_series(client)
        assert wait_for_task(
            client,
            series["initializationTask"]["taskId"],
        )["status"] == "failed"
        refreshed = client.get(f"/api/series/{series['id']}").json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]

        response = client.post(f"/api/sections/{section_id}/prepare")

        assert response.status_code == 502, response.json()
        assert response.json()["code"] == "CONTENT_ASSESSMENT_TARGET_UNBOUND"
        assert response.json()["retryable"] is True
        state = client.get(f"/api/sections/{section_id}").json()
        assert state["content"] is None
        assert state["quiz"] is None
        assert state["generation"]["status"] == "failed"


def test_v2_route_rejects_invalid_layout_before_formal_persistence():
    ai = V2FakeAi(invalid_layout=True)
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        task = wait_for_task(client, series["initializationTask"]["taskId"])
        with client.app.state.sessions() as db:
            run = db.scalar(select(GenerationRun).order_by(GenerationRun.started_at.desc()))
            assert task["status"] == "failed"
            assert run.error_code == "CONTENT_BLOCK_LAYOUT_INVALID"
            assert db.scalar(select(func.count()).select_from(ContentVersion)) == 0
            assert db.scalar(select(func.count()).select_from(QuizSet)) == 0


def test_legacy_content_never_claims_current_boundary_validation():
    ai = V2FakeAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        assert wait_for_task(
            client, series["initializationTask"]["taskId"]
        )["status"] == "succeeded"
        refreshed = client.get(f"/api/series/{series['id']}").json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
        lesson = client.get(f"/api/sections/{section_id}").json()
        with client.app.state.sessions() as db:
            content = db.get(ContentVersion, lesson["content"]["id"])
            content.schema_version = "legacy"
            content.prompt_version = "legacy"
            db.commit()

        legacy = client.get(f"/api/sections/{section_id}").json()
        assert legacy["content"]["boundaryValidation"] == {
            "status": "legacy",
            "ruleVersion": "lesson_candidate_gate_v12",
        }


def test_required_binding_failure_rolls_back_publication(monkeypatch):
    ai = V2FakeAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        assert wait_for_task(
            client, series["initializationTask"]["taskId"]
        )["status"] == "succeeded"
        refreshed = client.get(f"/api/series/{series['id']}").json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
        original = client.post(f"/api/sections/{section_id}/open").json()

        def fail_binding(*_args, **_kwargs):
            raise AppError(
                "注入绑定失败",
                code="TEST_BINDING_FAILURE",
                status=500,
            )

        monkeypatch.setattr(section_generation, "open_run_section", fail_binding)
        response = client.post(f"/api/sections/{section_id}/regenerate")
        assert response.status_code == 500
        assert response.json()["code"] == "TEST_BINDING_FAILURE"
        with client.app.state.sessions() as db:
            assert db.scalar(
                select(func.count())
                .select_from(ContentVersion)
                .where(ContentVersion.section_id == section_id)
            ) == 1
            latest_run = db.scalar(
                select(GenerationRun)
                .where(GenerationRun.section_id == section_id)
                .order_by(GenerationRun.attempt.desc())
            )
            assert latest_run.status == "failed"
            assert db.get(
                ContentVersion,
                original["content"]["id"],
            ).publication_status == "published"


def test_post_commit_read_failure_does_not_rewrite_successful_run(monkeypatch):
    ai = V2FakeAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        assert wait_for_task(
            client, series["initializationTask"]["taskId"]
        )["status"] == "succeeded"
        refreshed = client.get(f"/api/series/{series['id']}").json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
        client.post(f"/api/sections/{section_id}/open")

        def fail_read(self, requested_section_id):
            raise AppError(
                "注入发布后读取失败",
                code="TEST_READ_FAILURE",
                status=500,
            )

        monkeypatch.setattr(SlowService, "section", fail_read)
        response = client.post(f"/api/sections/{section_id}/regenerate")
        assert response.status_code == 500
        assert response.json()["code"] == "TEST_READ_FAILURE"
        with client.app.state.sessions() as db:
            latest_run = db.scalar(
                select(GenerationRun)
                .where(GenerationRun.section_id == section_id)
                .order_by(GenerationRun.attempt.desc())
            )
            assert latest_run.status == "succeeded"
            assert db.scalar(
                select(func.count())
                .select_from(ContentVersion)
                .where(
                    ContentVersion.section_id == section_id,
                    ContentVersion.publication_status == "published",
                )
            ) == 1


def test_v2_feedback_creates_a_new_atomic_content_and_quiz_version():
    ai = V2FakeAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = create_series(client)
        assert wait_for_task(
            client, series["initializationTask"]["taskId"]
        )["status"] == "succeeded"
        refreshed = client.get(f"/api/series/{series['id']}").json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
        original = client.post(f"/api/sections/{section_id}/open").json()
        block = original["content"]["blocks"][0]
        submitted = client.post(
            "/api/feedback",
            headers={"Idempotency-Key": "v2-feedback-atomic-001"},
            json={
                "scope": "content_block",
                "feedbackType": "unclear",
                "message": "请把中间机制讲清楚",
                "sectionId": section_id,
                "contentVersionId": original["content"]["id"],
                "blockId": block["id"],
            },
        )
        assert submitted.status_code == 201
        streamed = client.post(
            f"/api/feedback/{submitted.json()['id']}/repair/stream"
        )
        events = sse_events(streamed)
        done = next(payload for event, payload in events if event == "done")
        replacement = client.get(f"/api/sections/{section_id}").json()
        assert done["contentBlockId"] == replacement["content"]["blocks"][-1]["id"]
        assert done["contentBlockId"] != replacement["content"]["blocks"][0]["id"]
        replay = sse_events(
            client.post(f"/api/feedback/{submitted.json()['id']}/repair/stream")
        )
        replay_done = next(payload for event, payload in replay if event == "done")
        assert replay_done["replayed"] is True
        assert replay_done["contentBlockId"] == done["contentBlockId"]
        assert ai.generated_section_ids.count(section_id) == 2
        assert replacement["content"]["version"] == 2
        assert replacement["content"]["id"] != original["content"]["id"]
        assert replacement["quiz"]["id"] != original["quiz"]["id"]
        with client.app.state.sessions() as db:
            old_content = db.get(ContentVersion, original["content"]["id"])
            old_quiz = db.get(QuizSet, original["quiz"]["id"])
            assert old_content.publication_status == "superseded"
            assert old_quiz.publication_status == "superseded"
