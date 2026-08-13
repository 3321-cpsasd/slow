import asyncio

import pytest

from app.ai.gateway import (
    AiPurpose,
    ModelDeployment,
    ModelDeploymentRegistry,
    PurposeAiGateway,
    RoutePolicy,
    model_family,
)
from app.ai.port import ProviderCapabilities
from app.ai.contracts import (
    AskMeDiscussionEvaluation,
    AskMeDiscussionProbe,
    AskMeEvaluation,
    AskMeProbe,
    ChapterOutlineReviewBatch,
    ContentBlock,
    GeneratedChapter,
    GeneratedContent,
    GeneratedSectionOutline,
    GeneratedLessonSlotBlock,
    GeneratedLessonSlotContentCandidate,
    GeneratedLessonSlotQuestion,
    LessonQuestionAdjudicationBatch,
    LessonQuestionAuthorItem,
    LessonQuestionAuthorBatch,
    LessonQuestionReviewBatch,
)
from app.core.errors import AiError


class StubAdapter:
    configured = True

    def __init__(self, model):
        self.model = model
        self.calls = []
        self.capabilities = ProviderCapabilities(
            protocol="openai",
            api_mode="chat_completions",
            structured_output=True,
            streaming=True,
            reasoning_mode="optional",
        )

    async def generate_lesson(self, spec):
        self.calls.append(("generate_lesson", spec))
        return {"model": self.model}

    async def answer(self, request):
        self.calls.append(("answer", request))
        return {"model": self.model}

    async def answer_stream(self, request):
        self.calls.append(("answer_stream", request))
        yield self.model

    async def ask_me(self, request):
        self.calls.append(("ask_me", request))
        return {"model": self.model}

    def structured_trace(self):
        return []

    def set_usage_recorder(self, _recorder):
        return None

    async def close(self):
        return None

    async def check_connection(self):
        self.calls.append(("check_connection", None))


def gateway(*adapters, ask_ai=None, environment="test"):
    deployments = [
        ModelDeployment(
            deployment_id=f"deployment-{index}",
            provider_id="test",
            model=adapter.model,
            model_family_id=model_family(adapter.model),
            adapter=adapter,
            structured_mode="json_object",
        )
        for index, adapter in enumerate(adapters)
    ]
    deployment_ids = tuple(item.deployment_id for item in deployments)
    policies = {
        purpose.value: RoutePolicy(purpose.value, deployment_ids)
        for purpose in AiPurpose
    }
    if ask_ai:
        ask_deployment = next(
            item for item in deployments if item.adapter is ask_ai
        )
        policies[AiPurpose.ASK_AI.value] = RoutePolicy(
            AiPurpose.ASK_AI.value,
            (ask_deployment.deployment_id,),
        )
    return PurposeAiGateway(
        ModelDeploymentRegistry(deployments, environment=environment),
        policies,
        config_version_id="test-config-v1",
    )


def test_model_family_is_conservative_and_provider_independent():
    assert model_family("qwen3.8-max") == "qwen"
    assert model_family("deepseek-v4-pro") == "deepseek"
    assert model_family("glm-5.2") == "glm"
    assert model_family("vendor/custom") == "vendor"


def test_gateway_routes_ask_ai_without_exposing_model_to_caller():
    author = StubAdapter("qwen3.8-max")
    tutor = StubAdapter("qwen3.6-flash")
    router = gateway(author, tutor, ask_ai=tutor)

    result = asyncio.run(router.answer({"question": "为什么？"}))

    assert result == {"model": "qwen3.6-flash"}
    assert author.calls == []
    assert tutor.calls == [("answer", {"question": "为什么？"})]


def test_gateway_connection_check_covers_each_routed_deployment_once():
    author = StubAdapter("qwen3.8-max")
    evaluator = StubAdapter("glm-5.2")
    router = gateway(author, evaluator)

    asyncio.run(router.check_connection())

    assert author.calls == [("check_connection", None)]
    assert evaluator.calls == [("check_connection", None)]


def test_feedback_resolution_excludes_original_author_family():
    author = StubAdapter("qwen3.8-max")
    same_family = StubAdapter("qwen3.7-plus")
    independent = StubAdapter("glm-5.2")
    router = gateway(author, same_family, independent)
    spec = {"feedback": {
        "feedbackType": "inaccurate",
        "authorModel": "qwen3.8-max",
    }}

    async def run():
        result = await router.generate_lesson(spec)
        return result, router.last_model

    result, selected_model = asyncio.run(run())

    assert result == {"model": "glm-5.2"}
    assert author.calls == []
    assert same_family.calls == []
    assert independent.calls == [("generate_lesson", spec)]
    assert selected_model == "glm-5.2"


def test_feedback_resolution_fails_closed_without_independent_family():
    author = StubAdapter("qwen3.8-max")
    same_family = StubAdapter("qwen3.7-plus")
    router = gateway(author, same_family)

    with pytest.raises(AiError) as captured:
        asyncio.run(router.generate_lesson({
            "feedback": {
                "feedbackType": "inaccurate",
                "authorModel": "qwen3.8-max",
            }
        }))

    assert captured.value.code == "AI_ELIGIBLE_DEPLOYMENT_UNAVAILABLE"
    assert author.calls == []
    assert same_family.calls == []


def test_feedback_style_revision_can_reuse_original_author_family():
    author = StubAdapter("qwen3.8-max")
    router = gateway(author)
    spec = {"feedback": {
        "feedbackType": "unclear",
        "authorModel": "qwen3.8-max",
    }}

    result = asyncio.run(router.generate_lesson(spec))

    assert result == {"model": "qwen3.8-max"}
    assert author.calls == [("generate_lesson", spec)]


def test_independent_route_fails_closed_when_author_lineage_is_missing():
    author = StubAdapter("qwen3.8-max")
    independent = StubAdapter("glm-5.2")
    router = gateway(author, independent)

    with pytest.raises(AiError) as captured:
        asyncio.run(router.ask_me({"previousAnswer": "我的回答"}))

    assert captured.value.code == "AI_AUTHOR_LINEAGE_REQUIRED"
    assert author.calls == []
    assert independent.calls == []


def test_assessment_evaluation_excludes_author_family():
    author = StubAdapter("qwen3.8-max")
    same_family = StubAdapter("qwen3.7-plus")
    evaluator = StubAdapter("glm-5.2")
    router = gateway(author, same_family, evaluator)
    request = {
        "previousAnswer": "我的回答",
        "authorModelFamilyId": "qwen",
    }

    result = asyncio.run(router.ask_me(request))

    assert result == {"model": "glm-5.2"}
    assert author.calls == []
    assert same_family.calls == []
    assert evaluator.calls == [("ask_me", request)]


def test_structured_route_skips_prompt_only_deployment():
    prompt_only = StubAdapter("qwen3.8-max")
    native = StubAdapter("glm-5.2")
    deployments = [
        ModelDeployment(
            "prompt-only", "test", prompt_only.model, "qwen", prompt_only,
            structured_mode="prompt_json",
        ),
        ModelDeployment(
            "native", "test", native.model, "glm", native,
            structured_mode="json_object",
        ),
    ]
    ids = tuple(item.deployment_id for item in deployments)
    policies = {
        purpose.value: RoutePolicy(purpose.value, ids)
        for purpose in AiPurpose
    }
    router = PurposeAiGateway(
        ModelDeploymentRegistry(deployments, environment="test"),
        policies,
        config_version_id="test-config-v1",
    )

    result = asyncio.run(router.answer({"question": "为什么？"}))

    assert result == {"model": "glm-5.2"}
    assert prompt_only.calls == []


def test_route_without_required_structured_capability_is_rejected_at_startup():
    prompt_only = StubAdapter("qwen3.8-max")
    deployment = ModelDeployment(
        "prompt-only", "test", prompt_only.model, "qwen", prompt_only,
        structured_mode="prompt_json",
    )
    policies = {
        purpose.value: RoutePolicy(purpose.value, ("prompt-only",))
        for purpose in AiPurpose
    }

    with pytest.raises(ValueError, match="no structured deployment"):
        PurposeAiGateway(
            ModelDeploymentRegistry([deployment], environment="test"),
            policies,
            config_version_id="test-config-v1",
        )


def test_production_route_rejects_unapproved_backend():
    adapter = StubAdapter("qwen3.8-max")
    deployment = ModelDeployment(
        "unapproved", "test", adapter.model, "qwen", adapter,
        structured_mode="json_object",
        backend_allowed=False,
    )
    policies = {
        purpose.value: RoutePolicy(purpose.value, ("unapproved",))
        for purpose in AiPurpose
    }
    with pytest.raises(ValueError, match="no active deployment"):
        PurposeAiGateway(
            ModelDeploymentRegistry([deployment], environment="production"),
            policies,
            config_version_id="test-config-v1",
        )


def test_unknown_route_deployment_is_rejected_at_startup():
    adapter = StubAdapter("qwen3.8-max")
    deployment = ModelDeployment(
        "author", "test", adapter.model, "qwen", adapter,
        structured_mode="json_object",
    )
    policies = {
        purpose.value: RoutePolicy(purpose.value, ("author",))
        for purpose in AiPurpose
    }
    policies[AiPurpose.ASK_AI.value] = RoutePolicy(
        AiPurpose.ASK_AI.value,
        ("missing",),
    )

    with pytest.raises(ValueError, match="unknown deployment"):
        PurposeAiGateway(
            ModelDeploymentRegistry([deployment], environment="test"),
            policies,
            config_version_id="test-config-v1",
        )


def test_stream_does_not_splice_models_after_first_delta():
    class InterruptedAdapter(StubAdapter):
        async def answer_stream(self, request):
            self.calls.append(("answer_stream", request))
            yield "partial"
            raise AiError("interrupted", code="STREAM_INTERRUPTED", retryable=True)

    first = InterruptedAdapter("qwen3.8-max")
    fallback = StubAdapter("glm-5.2")
    router = gateway(first, fallback)

    async def consume():
        chunks = []
        with pytest.raises(AiError) as captured:
            async for chunk in router.answer_stream({"question": "为什么？"}):
                chunks.append(chunk)
        return chunks, captured.value

    chunks, error = asyncio.run(consume())

    assert chunks == ["partial"]
    assert error.code == "STREAM_INTERRUPTED"
    assert fallback.calls == []


def test_validated_single_model_route_still_runs_harness_validator():
    author = StubAdapter("qwen3.8-max")
    router = gateway(author)
    validated = []

    result = asyncio.run(router.generate_lesson_validated(
        {"feedback": {}},
        lambda candidate: validated.append(candidate),
    ))

    assert result == {"model": "qwen3.8-max"}
    assert validated == [result]


class TrustedAssessmentStub(StubAdapter):
    def __init__(self, model, role):
        super().__init__(model)
        self.role = role

    async def author_lesson_content(self, spec):
        self.calls.append(("author_lesson_content", spec))
        return GeneratedLessonSlotContentCandidate(
            blocks=[
                GeneratedLessonSlotBlock(
                    slot="T1_CORE",
                    kind="text",
                    primary_role="core_instruction",
                    heading="步长约束",
                    content="TMA 要求该维度的字节步长是 16 的整数倍；96 除以 16 等于 6，因此满足这个约束。",
                ),
                GeneratedLessonSlotBlock(
                    slot="S1",
                    kind="text",
                    primary_role="worked_example",
                    heading="直接计算",
                    content="把 96 字节除以 16 字节得到整数 6，可以直接确认这个步长满足整数倍约束。",
                ),
            ],
        )

    async def author_lesson_questions(self, payload):
        self.calls.append(("author_lesson_questions", payload))
        return LessonQuestionAuthorBatch(questions=[
            LessonQuestionAuthorItem(
                target_slot="T1",
                prompt=f"第 {index} 题：96 字节步长是否满足 16 字节倍数约束？",
                options=["满足", "不满足", "无法由 96 与 16 判断"],
            )
            for index in range(1, int(payload.get("questionCount", 4)) + 1)
        ])

    async def review_lesson_questions(self, payload):
        self.calls.append(("review_lesson_questions", payload))
        return LessonQuestionReviewBatch.model_validate({
            "questions": [
                {"item_slot": f"Q{index}", "decision": "accept"}
                for index in range(1, len(payload["questions"]) + 1)
            ]
        })

    async def adjudicate_lesson_questions(self, payload):
        self.calls.append(("adjudicate_lesson_questions", payload))
        return LessonQuestionAdjudicationBatch.model_validate({
            "questions": [
                {
                    "item_slot": f"Q{index}",
                    "option_verdicts": [
                        {"option_id": "O1", "decision": "satisfies", "evidence_slot": "T1_CORE", "rationale": "96 是 16 的 6 倍", "cause_code": ""},
                        {"option_id": "O2", "decision": "does_not_satisfy", "evidence_slot": "T1_CORE", "rationale": "与整数倍计算相反", "cause_code": "mechanism_reasoning_break"},
                        {"option_id": "O3", "decision": "does_not_satisfy", "evidence_slot": "T1_CORE", "rationale": "可直接由除法判断", "cause_code": "application_transfer_failure"},
                    ],
                }
                for index in range(1, len(payload["questions"]) + 1)
            ]
        })


def test_trusted_assessment_uses_three_distinct_model_families():
    content = TrustedAssessmentStub("qwen3.8-max", "content")
    item_author = TrustedAssessmentStub("deepseek-v4", "item")
    reviewer = TrustedAssessmentStub("glm-5.2", "review")
    adjudicator = TrustedAssessmentStub("claude-4", "adjudication")
    router = gateway(content, item_author, reviewer, adjudicator)
    router.policies[AiPurpose.LESSON_AUTHOR.value] = RoutePolicy(
        AiPurpose.LESSON_AUTHOR.value, ("deployment-0",)
    )
    router.policies[AiPurpose.ASSESSMENT_ITEM_AUTHOR.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ITEM_AUTHOR.value, ("deployment-1",)
    )
    router.policies[AiPurpose.ASSESSMENT_ITEM_REVIEW.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ITEM_REVIEW.value, ("deployment-1", "deployment-2")
    )
    router.policies[AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION.value,
        ("deployment-1", "deployment-2", "deployment-3"),
    )
    spec = {
        "feedback": {},
        "learningContractVersionId": "contract-1",
        "section": {"id": "section-1"},
        "targets": [{
            "assessmentTargetId": "target-1",
            "objective": "判断步长是否满足 16 字节整数倍约束",
            "required": True,
        }],
    }

    result = asyncio.run(router.generate_lesson(spec))

    assert all(question.answer_authority == "blind_model_adjudication_v1" for question in result.questions)
    assert [call[0] for call in content.calls] == ["author_lesson_content"]
    assert [call[0] for call in item_author.calls] == ["author_lesson_questions"]
    assert [call[0] for call in reviewer.calls] == ["review_lesson_questions"]
    assert [call[0] for call in adjudicator.calls] == ["adjudicate_lesson_questions"]


def test_lesson_falls_back_to_single_call_without_three_independent_families():
    content = TrustedAssessmentStub("qwen3.8-max", "content")
    item_author = TrustedAssessmentStub("deepseek-v4", "item")
    reviewer = TrustedAssessmentStub("glm-5.2", "review")
    router = gateway(content, item_author, reviewer)
    router.policies[AiPurpose.LESSON_AUTHOR.value] = RoutePolicy(
        AiPurpose.LESSON_AUTHOR.value, ("deployment-0",)
    )
    router.policies[AiPurpose.ASSESSMENT_ITEM_AUTHOR.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ITEM_AUTHOR.value, ("deployment-1",)
    )
    router.policies[AiPurpose.ASSESSMENT_ITEM_REVIEW.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ITEM_REVIEW.value, ("deployment-2",)
    )
    router.policies[AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION.value,
        ("deployment-1", "deployment-2"),
    )
    spec = {
        "feedback": {},
        "learningContractVersionId": "contract-1",
        "section": {"id": "section-1"},
        "targets": [{
            "assessmentTargetId": "target-1",
            "objective": "判断约束",
            "required": True,
        }],
    }

    result = asyncio.run(router.generate_lesson(spec))

    assert result == {"model": "qwen3.8-max"}
    assert [call[0] for call in content.calls] == ["generate_lesson"]
    assert item_author.calls == []
    assert reviewer.calls == []


def test_single_delayed_review_item_uses_the_same_trusted_pipeline():
    item_author = TrustedAssessmentStub("deepseek-v4", "item")
    reviewer = TrustedAssessmentStub("glm-5.2", "review")
    adjudicator = TrustedAssessmentStub("claude-4", "adjudication")
    router = gateway(item_author, reviewer, adjudicator)
    router.policies[AiPurpose.ASSESSMENT_ITEM_AUTHOR.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ITEM_AUTHOR.value, ("deployment-0",)
    )
    router.policies[AiPurpose.ASSESSMENT_ITEM_REVIEW.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ITEM_REVIEW.value,
        ("deployment-0", "deployment-1"),
    )
    router.policies[AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_ANSWER_ADJUDICATION.value,
        ("deployment-0", "deployment-1", "deployment-2"),
    )
    objective = "判断步长是否满足 16 字节整数倍约束"
    content = GeneratedContent(
        confidence="high",
        blocks=[
            ContentBlock(
                kind="text",
                role="core_instruction",
                heading="步长约束",
                content="TMA 要求步长是 16 字节的整数倍，96 除以 16 等于 6。",
                assessment_objectives=[objective],
            ),
            ContentBlock(
                kind="text",
                role="boundary",
                heading="约束边界",
                content="这个计算只验证步长条件，不替代其他容量与地址条件。",
                assessment_objectives=[objective],
            ),
        ],
    )
    prior = [{
        "assessmentTargetId": "target-1",
        "objective": objective,
        "core": True,
        "prompt": "旧题",
        "options": ["满足", "不满足", "无法判断"],
        "correct": [0],
    }]

    result = asyncio.run(router.lesson_quiz(
        {
            "id": "section-1",
            "reviewAssignmentId": "review-1",
            "learningContractVersionId": "contract-1",
        },
        content,
        prior,
    ))

    assert len(result.questions) == 1
    assert result.questions[0].answer_authority == "blind_model_adjudication_v1"
    assert result.questions[0].claim_block_indexes == []
    assert [call[0] for call in item_author.calls] == ["author_lesson_questions"]
    assert [call[0] for call in reviewer.calls] == ["review_lesson_questions"]
    assert [call[0] for call in adjudicator.calls] == [
        "adjudicate_lesson_questions"
    ]


class AskMeRoleStub(StubAdapter):
    async def ask_me_probe(self, request):
        self.calls.append(("ask_me_probe", request))
        return AskMeProbe(
            dimension=request["dimension"],
            prompt="请说明这个边界条件为什么会改变结论？",
        )

    async def evaluate_ask_me(self, request):
        self.calls.append(("evaluate_ask_me", request))
        return AskMeEvaluation(
            dimension=request["evaluatesDimension"],
            evaluation="partial",
            rationale="回答提到了机制，但缺少关键边界条件。",
            evidence_sufficiency="insufficient",
        )

    async def evaluate_ask_me_discussion(self, request):
        self.calls.append(("evaluate_ask_me_discussion", request))
        return AskMeDiscussionEvaluation(
            evaluation="partial",
            correct_points=["识别了核心机制"],
            issues=[],
            suggestions=["检查失效条件"],
            topic_sufficiency="insufficient",
        )

    async def ask_me_discussion_probe(self, request):
        self.calls.append(("ask_me_discussion_probe", request))
        return AskMeDiscussionProbe(
            follow_up_prompt="什么条件会让刚才的判断失效？",
            follow_up_purpose="继续探测适用边界。",
        )


def test_ask_me_evaluator_excludes_content_author_and_previous_probe_family():
    author = AskMeRoleStub("qwen3.8-max")
    probe = AskMeRoleStub("deepseek-v4")
    evaluator = AskMeRoleStub("glm-5.2")
    router = gateway(author, probe, evaluator)
    router.policies[AiPurpose.ASSESSMENT_EVALUATION.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_EVALUATION.value,
        ("deployment-0", "deployment-1", "deployment-2"),
    )
    router.policies[AiPurpose.ASSESSMENT_PROBE.value] = RoutePolicy(
        AiPurpose.ASSESSMENT_PROBE.value,
        ("deployment-0", "deployment-1", "deployment-2"),
    )

    result = asyncio.run(router.ask_me({
        "dimension": "boundary",
        "evaluatesDimension": "mechanism",
        "previousPrompt": "机制是什么？",
        "previousAnswer": "它通过资源约束参与调度。",
        "finalize": False,
        "authorDeploymentId": "deployment-0",
        "authorModelFamilyId": "qwen",
        "probeDeploymentId": "deployment-1",
        "probeModelFamilyId": "deepseek",
    }))

    assert result.dimension == "boundary"
    assert result.evaluation == "partial"
    assert author.calls == []
    assert [call[0] for call in evaluator.calls] == ["evaluate_ask_me"]
    assert [call[0] for call in probe.calls] == ["ask_me_probe"]


class OutlineReviewStub(StubAdapter):
    async def chapter(self, request, memory):
        self.calls.append(("chapter", request))
        return GeneratedChapter(sections=[
            GeneratedSectionOutline(
                title="资源请求与限制：CPU 到 GPU 独占",
                question="CPU 与 GPU 的请求和限制语义有何不同？",
                objectives=["解释 GPU 独占资源的申请与调度语义"],
                baseline_concept_key="gpu-resource-semantics",
                baseline_objective_key="gpu-resource-objective",
            ),
            GeneratedSectionOutline(
                title="GPU 的资源表示：nvidia.com/gpu",
                question="GPU 在 Kubernetes 中如何申请？",
                objectives=["说明 nvidia.com/gpu 的申请语义"],
                baseline_concept_key="gpu-sharing-boundaries",
                baseline_objective_key="gpu-sharing-objective",
            ),
        ])

    async def review_chapter_outline(self, payload):
        self.calls.append(("review_chapter_outline", payload))
        return ChapterOutlineReviewBatch.model_validate({
            "sections": [
                {"section_slot": "S1", "decision": "accept"},
                {
                    "section_slot": "S2",
                    "decision": "edit",
                    "issues": ["adjacent_scope_overlap"],
                    "edit": {
                        "title": "GPU 切分与共享：MIG、time-slicing 与显存隔离",
                        "question": "GPU 被切分或共享时，隔离与调度边界如何变化？",
                        "objectives": ["比较 MIG、time-slicing 与显存隔离的资源语义"],
                    },
                },
            ]
        })


def test_independent_outline_reviewer_minimally_edits_overlapping_section():
    author = OutlineReviewStub("qwen3.8-max")
    reviewer = OutlineReviewStub("glm-5.2")
    router = gateway(author, reviewer)
    router.policies[AiPurpose.CURRICULUM.value] = RoutePolicy(
        AiPurpose.CURRICULUM.value, ("deployment-0",)
    )
    router.policies[AiPurpose.CURRICULUM_REVIEW.value] = RoutePolicy(
        AiPurpose.CURRICULUM_REVIEW.value, ("deployment-0", "deployment-1")
    )

    result = asyncio.run(router.chapter(
        {"title": "AI 资源模型", "objective": "理解异构资源调度"},
        [],
    ))

    assert result.sections[0].title.startswith("资源请求与限制")
    assert result.sections[1].title.startswith("GPU 切分与共享")
    assert result.sections[1].baseline_concept_key == "gpu-sharing-boundaries"
    assert result.sections[1].baseline_objective_key == "gpu-sharing-objective"
    assert [call[0] for call in author.calls] == ["chapter"]
    assert [call[0] for call in reviewer.calls] == ["review_chapter_outline"]


def test_outline_reviewer_schema_cannot_replace_or_reidentify_sections():
    with pytest.raises(ValueError):
        ChapterOutlineReviewBatch.model_validate({
            "sections": [
                {"section_slot": "S1", "decision": "accept"},
                {"section_slot": "S2", "decision": "reject"},
            ]
        })

    with pytest.raises(ValueError):
        ChapterOutlineReviewBatch.model_validate({
            "sections": [
                {"section_slot": "S1", "decision": "accept"},
                {
                    "section_slot": "S2",
                    "decision": "edit",
                    "issues": ["adjacent_scope_overlap"],
                    "edit": {
                        "title": "新的范围",
                        "baseline_concept_key": "replacement-concept",
                    },
                },
            ]
        })


def test_outline_review_unavailable_does_not_fail_chapter_generation():
    author = OutlineReviewStub("qwen3.8-max")
    router = gateway(author)

    async def generate_and_capture_trace():
        result = await router.chapter(
            {"title": "AI 资源模型", "objective": "理解异构资源调度"},
            [],
        )
        return result, router.fallback_trace()

    result, trace = asyncio.run(generate_and_capture_trace())

    assert result.sections[1].title.startswith("GPU 的资源表示")
    assert trace[-1]["outcome"] == "skipped"
    assert trace[-1]["errorCode"] == "AI_ELIGIBLE_DEPLOYMENT_UNAVAILABLE"


def test_production_does_not_publish_an_unreviewed_chapter_outline():
    author = OutlineReviewStub("qwen3.8-max")
    router = gateway(author, environment="production")

    with pytest.raises(AiError) as raised:
        asyncio.run(router.chapter(
            {"title": "AI 资源模型", "objective": "理解异构资源调度"},
            [],
        ))

    assert raised.value.code == "AI_CURRICULUM_REVIEW_REQUIRED"
    assert raised.value.retryable is True
