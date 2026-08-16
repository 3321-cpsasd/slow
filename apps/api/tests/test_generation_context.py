import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai.contracts import (
    GeneratedContent,
    GeneratedQuiz,
    GeneratedRemediationContent,
    LessonAlignmentIssue,
    LessonAlignmentReview,
    TeachingBlueprint,
    TeachingBlueprintBlock,
)
from app.application.generation_context import GenerationContextBuilder
from app.core.errors import AppError
from app.infrastructure.tables import (
    ContentVersion,
    GenerationRun,
    LearningContractVersion,
    Shelf,
    UserProfile,
)
from app.main import create_app
from app.services.source_verifier import AcceptingSourceVerifier

from test_vertical_slice import FakeAi, wait_for_task


class ContextRecordingAi(FakeAi):
    staged_lesson_generation = True

    def __init__(self):
        self.requests: dict[str, list[dict]] = {
            "plan": [],
            "chapter": [],
            "blueprint": [],
            "content": [],
            "quiz": [],
            "alignment": [],
        }

    async def plan(self, request, memory):
        self.requests["plan"].append(request)
        return await super().plan(request, memory)

    async def chapter(self, request, memory):
        self.requests["chapter"].append(request)
        return await super().chapter(request, memory)

    async def teaching_blueprint(self, request, memory):
        self.requests["blueprint"].append(request)
        return TeachingBlueprint(
            narrative_thread="从招聘判断问题出发，沿同一候选人案例解释机制与边界。",
            opening_move="先判断一位候选人能否解决给定业务问题。",
            recurring_example="同一位候选人的项目经历与业务约束。",
            core_model="用任务证据、作用机制和适用边界共同判断能力，而不是只看术语。",
            recap_prompt="复述判断所需的三类证据和一个失效边界。",
            preference_applications=["采用问题先行；图解仅在关系复杂时使用"],
            blocks=[
                TeachingBlueprintBlock(kind="text", role="conclusion", purpose="建立判断方向", heading_intent="先判断这份经历说明了什么"),
                TeachingBlueprintBlock(kind="text", role="mechanism", purpose="解释能力证据链", heading_intent="从任务走到能力的证据链"),
                TeachingBlueprintBlock(kind="text", role="example", purpose="推进候选人案例", heading_intent="把证据链放进候选人案例"),
                TeachingBlueprintBlock(kind="text", role="boundary", purpose="识别判断失效边界", heading_intent="哪些经历还不能证明能力"),
                TeachingBlueprintBlock(kind="text", role="practice", purpose="复述并迁移判断模型", heading_intent="换一份简历再做判断"),
            ],
        )

    async def lesson_content(self, request, memory, prior_questions=None):
        self.requests["content"].append(request)
        lesson = await super().lesson(request, memory, prior_questions)
        schema = (
            GeneratedRemediationContent
            if request.get("remediationStrategy")
            else GeneratedContent
        )
        return schema(
            confidence=lesson.confidence,
            sources=lesson.sources,
            blocks=lesson.blocks,
        )

    async def repair_lesson_sources(
        self,
        request,
        memory,
        content,
        failed_sources,
        prior_questions=None,
    ):
        return content

    async def lesson_quiz(self, request, content, prior_questions=None):
        self.requests["quiz"].append(request)
        lesson = await super().lesson(request, [], prior_questions)
        return GeneratedQuiz(questions=lesson.questions)

    async def review_lesson_alignment(self, request, content, quiz):
        self.requests["alignment"].append(request)
        objectives = [
            str(item.get("statement") or item.get("objective") or "")
            if isinstance(item, dict)
            else str(item)
            for item in (request.get("objectives") or [request["question"]])
        ]
        return LessonAlignmentReview(
            allowed=True,
            issues=[],
            covered_objectives=objectives,
        )


class RejectingAlignmentAi(ContextRecordingAi):
    async def review_lesson_alignment(self, request, content, quiz):
        self.requests["alignment"].append(request)
        return LessonAlignmentReview(
            allowed=False,
            issues=[
                LessonAlignmentIssue(
                    code="question_not_answered",
                    severity="blocking",
                    message="核心结论没有回答小节问题",
                    block_indexes=[0],
                )
            ],
            covered_objectives=[],
        )


def _create(client):
    response = client.post(
        "/api/plans",
        json={
            "shelfId": "shelf_technology",
            "topic": "大模型招聘判断",
            "role": "猎头顾问",
            "experience": "能阅读技术简历，但不负责编写训练代码",
            "purpose": "判断候选人能解决什么业务场景",
            "depth": "deep",
            "details": "避免默认成算法工程师",
        },
    )
    assert response.status_code == 201, response.json()
    return response.json()


def test_context_pack_propagates_profile_mission_depth_and_attempt():
    ai = ContextRecordingAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        with client.app.state.sessions() as db:
            profile = db.get(UserProfile, "user_demo")
            profile.preferences_json = json.dumps(
                {
                    "openingStyle": "problem_first",
                    "explanationDensity": "thorough",
                    "formatPreferences": ["worked_example", "diagram"],
                    "interactionRhythm": "balanced",
                    "dailyModePromptEnabled": False,
                },
                ensure_ascii=False,
            )
            db.commit()
        series = _create(client)
        task = wait_for_task(client, series["initializationTask"]["taskId"])
        assert task["status"] == "succeeded"

        plan_context = ai.requests["plan"][0]["generationContext"]
        assert plan_context["operation"] == "plan"
        assert plan_context["learner"]["planRole"] == "猎头顾问"
        assert plan_context["learner"]["planExperience"].startswith("能阅读技术简历")
        assert plan_context["policy"]["depthPolicy"]["label"] == "深入理解"

        chapter_context = ai.requests["chapter"][0]["generationContext"]
        assert chapter_context["operation"] == "chapter"
        assert chapter_context["learner"]["planRole"] == "猎头顾问"
        assert chapter_context["mission"]["why"] == "判断候选人能解决什么业务场景"
        assert chapter_context["curriculum"]["book"]["title"]

        content_request = ai.requests["content"][0]
        blueprint_request = ai.requests["blueprint"][0]
        assert blueprint_request["generationContext"]["learner"]["preferences"] == {
            "openingStyle": "problem_first",
            "explanationDensity": "thorough",
            "formatPreferences": ["worked_example", "diagram"],
            "interactionRhythm": "balanced",
        }
        assert content_request["teachingBlueprint"]["version"] == "teaching_blueprint_v1"
        assert content_request["teachingBlueprint"]["recurring_example"]
        with client.app.state.sessions() as db:
            generation_run = db.scalar(
                select(GenerationRun).where(
                    GenerationRun.operation == "lesson",
                    GenerationRun.status == "succeeded",
                )
            )
            generation_trace = json.loads(generation_run.trace_json)
            assert generation_trace["knowledgeContext"]["status"] == "not_applicable"
            assert generation_trace["knowledgeContext"]["retrievalRuleVersion"]
            assert generation_trace["knowledgeContext"]["actual"] == {
                "nodeCount": 0,
                "edgeCount": 0,
                "claimCount": 0,
            }
            assert (
                generation_trace["generationVariant"]
                == "preference_aware_blueprint_v1"
            )
            assert (
                generation_trace["teachingBlueprint"]["version"]
                == "teaching_blueprint_v1"
            )
        content_context = content_request["generationContext"]
        assert content_context["operation"] == "lesson_content"
        assert content_context["knowledgeContext"]["status"] == "not_applicable"
        assert content_context["learner"]["planRole"] == "猎头顾问"
        assert content_context["mission"]["constraints"]["depth"] == "deep"
        assert content_context["curriculum"]["chapter"]["objective"]
        assert content_context["curriculum"]["section"]["question"]
        assert content_context["learningContract"]["targetDepth"] == "deep"
        assert content_context["learningContract"]["targets"]
        assert all(
            target["targetDepth"] == "deep"
            for target in content_context["learningContract"]["targets"]
        )
        assert all(
            target["assessmentLevel"] == "standard"
            for target in content_context["learningContract"]["targets"]
        )
        assert all(
            target["targetDepth"] == "deep"
            for target in content_request["assessmentTargets"]
        )

        quiz_context = ai.requests["quiz"][0]["generationContext"]
        assert quiz_context["operation"] == "lesson_quiz"
        assert "change correctness" in " ".join(
            quiz_context["policy"]["forbiddenUses"]
        )
        assert ai.requests["alignment"][0]["generationContext"]["learner"][
            "planRole"
        ] == "猎头顾问"

        refreshed = client.get(f"/api/series/{series['id']}").json()
        first = refreshed["books"][0]["chapters"][0]["sections"][0]
        section = client.get(f"/api/sections/{first['id']}").json()
        attempt = client.post(
            f"/api/sections/{first['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[0] for _ in section["quiz"]["questions"]],
            },
        ).json()
        remediation_task = next(
            item
            for item in attempt["workflowTasks"]
            if item["type"] == "remediation_generation"
        )
        assert wait_for_task(client, remediation_task["taskId"])["status"] == "succeeded"
        remediation_request = next(
            item
            for item in ai.requests["content"]
            if item.get("remediationStrategy")
        )
        remediation_context = remediation_request["generationContext"]
        assert remediation_context["operation"] == "remediation"
        assert remediation_context["learningState"]["attempt"]["answers"]
        assert remediation_context["learningState"]["attempt"]["scoringResults"]
        assert remediation_context["learner"]["planRole"] == "猎头顾问"
        assert not any(
            request.get("remediationStrategy")
            for request in ai.requests["alignment"]
        )

        with client.app.state.sessions() as db:
            contract = db.scalar(select(LearningContractVersion))
            assert contract.target_depth == "deep"


def test_semantic_alignment_gate_fails_closed_without_persisting_content():
    ai = RejectingAlignmentAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        )
    ) as client:
        series = _create(client)
        task = wait_for_task(
            client,
            series["initializationTask"]["taskId"],
            timeout=5,
        )
        assert task["status"] == "failed"
        assert task["errorCode"] == "LESSON_SEMANTIC_ALIGNMENT_FAILED"
        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(ContentVersion)) == 0


def test_context_builder_rejects_cross_user_shelf():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ContextRecordingAi(),
            AcceptingSourceVerifier(),
        )
    ) as client:
        with client.app.state.sessions() as db:
            shelf = db.get(Shelf, "shelf_technology")
            builder = GenerationContextBuilder(db, user_id="user_other")
            with pytest.raises(AppError) as raised:
                builder.build(
                    "plan",
                    shelf=shelf,
                    memory=[],
                    plan_input={
                        "role": "猎头顾问",
                        "experience": "能阅读技术简历",
                        "purpose": "判断候选人能力",
                        "depth": "deep",
                    },
                )
            assert raised.value.code == "GENERATION_CONTEXT_OWNER_MISMATCH"
            assert raised.value.status == 403
