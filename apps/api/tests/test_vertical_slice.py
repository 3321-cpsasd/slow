import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from app.ai.contracts import AskMeDiscussionTurn, AskMeTurn, ClassifiedAnswer, ContentBlock, GeneratedChapter, GeneratedContent, GeneratedLesson, GeneratedNote, GeneratedPlan, GeneratedQuiz, GeneratedSectionOutline, PlanBook, PlanChapter, PlanMilestone, PlanMilestoneCriterion, ReplannedBook, ReplannedChapter, Source, ChoiceQuestion
from app.application.service import (
    apply_source_repair_scope,
    source_blacklist_from_generation_traces,
)
from app.domain.learning import grade_choice_quiz
from app.main import create_app
from app.core.errors import AiError
from app.evaluation.runner import (
    _evaluation_checks,
    _semantic_evidence,
    _snapshot_database_facts,
    run,
)
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import (
    AcceptingSourceVerifier,
    SourceVerificationError,
    Verification,
)
from app.infrastructure.tables import (
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    AssessmentGateState,
    AssessmentObservation,
    AssessmentTarget,
    AskMeDiscussionSession,
    AskMeDiscussionTopic,
    AskMeDiscussionTurnRecord,
    Book,
    ChapterRevision,
    ChapterChallengeAttempt,
    ChapterRouteDecisionEvent,
    ContentBlockClaimAnchor,
    ContentBlockVersion,
    ContentVersion,
    EvidenceQualificationEvent,
    GenerationRun,
    GovernanceDecisionSnapshot,
    KnowledgeGap,
    KnowledgeStateProjection,
    LearningEvidence,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    LearningDecisionSnapshot,
    LearningNote,
    LearningNoteReviewSupplement,
    LearningNoteSummary,
    LearningNoteUserRevision,
    LearningTask,
    LearningPlan,
    LearningRun,
    LearningRunSectionBinding,
    LearningStartPreview,
    MilestonePath,
    MilestonePathRevision,
    QuizAttempt,
    QuizSet,
    QaSession,
    ReviewState,
    ScoringResult,
    Remediation,
    Section,
    SectionAssessmentTarget,
    SectionProgress,
    Series,
    SeriesLearningStartPreference,
    SourceClaimBinding,
    SourceClaimVersion,
    SourceVersion,
    UserFeedback,
    UserDailyModeState,
    now,
)
from app.modules.learning.assessment import (
    bind_questions_to_targets,
    rebuild_assessment_projections,
)
from app.modules.learning.assessment_items import publish_assessment_item_versions
from app.modules.learning.contracts import ensure_learning_contract
from app.modules.learning.content_governance_store import (
    record_verified_claim_binding,
    reevaluate_generated_governance,
)
from app.modules.learning.rebuild import rebuild_user_projections


pytestmark = pytest.mark.api_vertical


class FakeAi:
    configured, model = True, "fake-structured"
    allow_legacy_lesson_generation_for_tests = True
    async def close(self): pass
    async def plan(self, request, memory):
        return GeneratedPlan(
            series_title="K8s 台阶",
            rationale="从对象到排障",
            assumptions=[],
            confidence="high",
            books=[
                PlanBook(
                    title="K8s（一）基础",
                    topic="K8s",
                    description="核心对象",
                    estimated_minutes=300,
                    chapters=[
                        PlanChapter(title="Pod 与调度", objective="理解对象和调度"),
                        PlanChapter(title="部署", objective="完成基础部署"),
                    ],
                ),
                PlanBook(
                    title="K8s（二）网络",
                    topic="K8s 网络",
                    description="网络与服务",
                    estimated_minutes=300,
                    chapters=[
                        PlanChapter(title="Service", objective="理解服务发现"),
                        PlanChapter(title="排障", objective="定位网络问题"),
                    ],
                ),
            ],
            milestones=[
                PlanMilestone(
                    title="解释核心对象与调度",
                    outcome="能够解释对象关系和调度机制",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement="理解对象和调度",
                            book_position=1,
                            chapter_position=1,
                        )
                    ],
                ),
                PlanMilestone(
                    title="完成部署并理解服务发现",
                    outcome="能够从部署推进到网络服务发现",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement="完成基础部署",
                            book_position=1,
                            chapter_position=2,
                        ),
                        PlanMilestoneCriterion(
                            statement="理解服务发现",
                            book_position=2,
                            chapter_position=1,
                        ),
                    ],
                ),
                PlanMilestone(
                    title="定位网络问题",
                    outcome="能够综合定位网络边界问题",
                    criteria=[
                        PlanMilestoneCriterion(
                            statement="定位网络问题",
                            book_position=2,
                            chapter_position=2,
                        )
                    ],
                ),
            ],
        )
    async def chapter(self, request, memory):
        return GeneratedChapter(sections=[GeneratedSectionOutline(title=f"第{i}节", question=f"问题{i}", objectives=[f"目标{i}"]) for i in range(1,4)])
    async def lesson(self, request, memory, prior_questions=None):
        self.last_lesson_request = request
        generation = 1
        if prior_questions:
            previous_prefix = prior_questions[0]["prompt"].split("套", 1)[0]
            generation = int(previous_prefix.removeprefix("第")) + 1
        roles = ["conclusion","mechanism","example","boundary","practice"]
        objectives = request.get("objectives") or [request["question"]]
        question_count = len(prior_questions) if prior_questions else 5
        return GeneratedLesson(
            confidence="high",
            sources=[Source(
                title="Kubernetes Docs",
                url="https://kubernetes.io/docs/",
                kind="official",
                version="v1.30",
            )],
            blocks=[
                ContentBlock(
                    kind="text",
                    role=role,
                    heading=f"{role} 完整说明",
                    content=(
                        f"{role} 内容用于解释当前目标的核心机制、观察依据和适用边界。"
                        "学习者需要把对象之间的关系说清楚，并能用一个反例检查结论是否仍然成立。"
                        "完成阅读后，再通过选择题验证自己是否真正掌握这一判断方法。"
                    ),
                    source_indexes=[0],
                    assessment_objectives=objectives,
                )
                for role in roles
            ],
            questions=[
                ChoiceQuestion(
                    prompt=f"第{generation}套题{i}",
                    options=[
                        f"A{generation}",
                        f"B{generation}",
                        f"C{generation}",
                    ],
                    correct=[1],
                    core=i == 0,
                    objective=objectives[i % len(objectives)],
                    explanation=f"因为 B{generation}",
                    claim_block_indexes=[0],
                )
                for i in range(question_count)
            ],
        )
    async def answer(self, request):
        requested = request.get("requestedThreadId")
        return ClassifiedAnswer(relation="follow_up" if requested else "new_question", thread_id=request.get("newThreadId") or requested, answer="基于当前段落回答", thread_summary="已澄清机制")
    async def repair_stream(self, request):
        self.last_repair_request = request
        for chunk in [
            "| 维度 | 数据可视化 | 信息图表 |\n",
            "| --- | --- | --- |\n",
            "| 目的 | 探索规律 | 传递结论 |\n\n",
            "表格之外的说明现在是独立段落。",
        ]:
            yield chunk
    async def note(self, request):
        return GeneratedNote(solved_question="解决问题", core_mechanism=["机制"], personal_gaps=[], boundaries=["边界"], practice_checks=["检查"], sources=["Kubernetes Docs"], unresolved=[])
    async def ask_me(self, request):
        dimension = request["dimension"]
        return AskMeTurn(dimension=dimension, prompt=f"请说明 {dimension}", evaluation="not_evaluated" if not request.get("previousAnswer") else "strong", rationale="回答覆盖关键点")
    async def ask_me_discussion(self, request):
        return AskMeDiscussionTurn(
            evaluation="partial",
            correct_points=["回答已经提出了一个可判断的观点。"],
            issues=[{
                "kind": "evidence_insufficient",
                "answer_excerpt": request["previousAnswer"],
                "explanation": "还需要一个可以验证判断的具体信号。",
            }],
            suggestions=["补充一个业务或技术层面的可观察证据。"],
            follow_up_prompt="什么证据会让你改变刚才的判断？",
            follow_up_purpose="检查判断边界是否稳定。",
            topic_sufficiency="insufficient",
        )
    async def replan_book(self, request, memory):
        self.last_replan_request = request
        return ReplannedBook(rationale="根据学习记忆减少重复", chapters=[ReplannedChapter(title="重规划章节", objective="验证迁移")])


class ParallelWorkflowAi(FakeAi):
    def __init__(self):
        self.parallel_phase = False
        self.note_started = Event()
        self.release_note = Event()
        self.events: list[str] = []

    async def note(self, request):
        if self.parallel_phase:
            self.events.append("note_start")
            self.note_started.set()
            released = await asyncio.to_thread(self.release_note.wait, 3)
            if not released:
                raise TimeoutError("test did not release note generation")
            self.events.append("note_end")
        return await super().note(request)


class MissingLineageAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        lesson = await super().lesson(request, memory, prior_questions)
        for block in lesson.blocks:
            block.assessment_objectives = []
        for question in lesson.questions:
            question.claim_block_indexes = []
        return lesson


class FeedbackRegenerationFailingAi(FakeAi):
    async def repair_stream(self, request):
        raise AiError(
            "反馈修订生成失败",
            code="FEEDBACK_REPAIR_TEST_FAILURE",
            retryable=False,
        )
        yield  # pragma: no cover


def test_quiz_grade_explains_missed_and_incorrect_multiselect_options():
    grade = grade_choice_quiz(
        [
            {
                "correct": [0, 2],
                "core": True,
                "objective": "识别必要条件",
                "explanation": "A 和 C 都是必要条件。",
            }
        ],
        [[0, 1]],
    )

    assert grade.passed is False
    assert grade.results == [
        {
            "correct": False,
            "explanation": "A 和 C 都是必要条件。",
            "objective": "识别必要条件",
            "selectedOptions": [0, 1],
            "correctOptions": [0, 2],
            "missedOptions": [2],
            "incorrectOptions": [1],
        }
    ]


def test_quiz_grade_requires_eighty_percent_and_core_resolution():
    questions = [
        {
            "correct": [1],
            "core": index == 0,
            "objective": f"目标{index}",
            "explanation": f"解析{index}",
        }
        for index in range(5)
    ]

    grade = grade_choice_quiz(
        questions,
        [[0], [0], [1], [1], [1]],
    )

    assert grade.score == 3
    assert grade.passed is False
    assert grade.perfect is False

    core_failed = grade_choice_quiz(
        questions,
        [[0], [1], [1], [1], [1]],
    )
    assert core_failed.score == 4
    assert core_failed.passed is False

    passed = grade_choice_quiz(
        questions,
        [[1], [0], [1], [1], [1]],
    )
    assert passed.score == 4
    assert passed.passed is True


class StagedFakeAi(FakeAi):
    staged_lesson_generation = True

    def __init__(self, events):
        self.events = events

    async def lesson_content(self, request, memory, prior_questions=None):
        self.events.append("content")
        lesson = await super().lesson(request, memory, prior_questions)
        return GeneratedContent(
            confidence=lesson.confidence,
            sources=lesson.sources,
            blocks=lesson.blocks,
        )

    async def repair_lesson_sources(self, request, memory, content, failed_sources, prior_questions=None):
        self.events.append("repair")
        return content

    async def lesson_quiz(self, request, content, prior_questions=None):
        self.events.append("quiz")
        lesson = await super().lesson(request, [], prior_questions)
        return GeneratedQuiz(questions=lesson.questions)


class RecordingVerifier(AcceptingSourceVerifier):
    def __init__(self, events):
        self.events = events

    async def verify(self, sources):
        self.events.append("verify")
        return await super().verify(sources)


class ReachabilityOnlyVerifier:
    """Production-shaped verifier: URL checks, but no claim-verification writer."""

    async def verify(self, sources):
        return await AcceptingSourceVerifier().verify(sources)


class WriteProbeVerifier(AcceptingSourceVerifier):
    """Proves claim I/O starts without an outer SQLite write transaction."""

    def __init__(self, database_path):
        self.database_path = database_path
        self.probed = False

    async def verify_claims(self, candidates):
        with sqlite3.connect(self.database_path, timeout=0.1) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE users SET updated_at = updated_at WHERE id = ?",
                ("user_demo",),
            )
            connection.commit()
        self.probed = True
        return await super().verify_claims(candidates)


class FailingClaimVerifier(AcceptingSourceVerifier):
    async def verify_claims(self, _candidates):
        raise AiError(
            "来源语义核验失败",
            code="SOURCE_CLAIM_REVIEW_FAILED",
            retryable=False,
        )


MISSING_SOURCE_URL = "https://docs.missing.example/2025/removed"
WORKING_SOURCE_URL = "https://docs.working.example/guide"


class MissingSourceVerifier:
    async def verify(self, sources):
        results = []
        failures = []
        for source in sources:
            verification = Verification(
                url=source.url,
                reachable=source.url != MISSING_SOURCE_URL,
                status_code=(
                    404 if source.url == MISSING_SOURCE_URL else 200
                ),
                pinned=True,
            )
            results.append(verification)
            if not verification.reachable:
                failures.append(verification)
        if failures:
            raise SourceVerificationError(failures, results=results)
        return [item.as_dict() for item in results]


class RecoveringSourceRepairAi(StagedFakeAi):
    def __init__(self):
        super().__init__([])
        self.repair_requests = []

    async def lesson_content(self, request, memory, prior_questions=None):
        content = await super().lesson_content(
            request,
            memory,
            prior_questions,
        )
        content.sources[0] = Source(
            title="Missing",
            url=MISSING_SOURCE_URL,
            kind="official",
            version="2025",
        )
        return content

    async def repair_lesson_sources(
        self,
        request,
        memory,
        content,
        failed_sources,
        prior_questions=None,
    ):
        self.repair_requests.append({
            "urls": list(request["rejectedSourceUrls"]),
            "hosts": list(request["rejectedSourceHosts"]),
        })
        if len(self.repair_requests) == 1:
            raise AiError(
                "repair reused a rejected source",
                code="SOURCE_REPAIR_SCOPE_VIOLATION",
            )
        repaired = content.model_copy(deep=True)
        repaired.sources[0] = Source(
            title="Working",
            url=WORKING_SOURCE_URL,
            kind="official",
            version="current",
        )
        return repaired


class RetryBlacklistAi(StagedFakeAi):
    def __init__(self):
        super().__init__([])
        self.content_requests = []
        self.quiz_requests = []

    async def lesson_content(self, request, memory, prior_questions=None):
        self.content_requests.append({
            "urls": list(request["rejectedSourceUrls"]),
            "hosts": list(request["rejectedSourceHosts"]),
        })
        content = await super().lesson_content(
            request,
            memory,
            prior_questions,
        )
        blacklisted = "docs.missing.example" in request[
            "rejectedSourceHosts"
        ]
        content.sources[0] = Source(
            title="Working" if blacklisted else "Missing",
            url=WORKING_SOURCE_URL if blacklisted else MISSING_SOURCE_URL,
            kind="official",
            version="current" if blacklisted else "2025",
        )
        return content

    async def repair_lesson_sources(
        self,
        request,
        memory,
        content,
        failed_sources,
        prior_questions=None,
    ):
        raise AiError(
            "repair reused a rejected source",
            code="SOURCE_REPAIR_SCOPE_VIOLATION",
        )

    async def lesson_quiz(
        self,
        request,
        content,
        prior_questions=None,
    ):
        self.quiz_requests.append({
            "unverifiedSourceIndexes": list(
                request.get("unverifiedSourceIndexes", [])
            ),
            "contentReliability": request.get("contentReliability"),
        })
        return await super().lesson_quiz(
            request,
            content,
            prior_questions,
        )


def test_source_blacklist_carries_only_permanent_not_found_failures():
    urls, hosts = source_blacklist_from_generation_traces([
        {
            "stageHistory": [
                {
                    "stage": "source_repair",
                    "failedSources": [
                        {
                            "url": MISSING_SOURCE_URL,
                            "reason": "not_found",
                        },
                        {
                            "url": "https://temporary.example/guide",
                            "reason": "network_error",
                        },
                    ],
                }
            ]
        }
    ])

    assert urls == [MISSING_SOURCE_URL]
    assert hosts == ["docs.missing.example"]


def test_default_model_only_route_never_runs_source_repair():
    ai = RecoveringSourceRepairAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            MissingSourceVerifier(),
        )
    ) as source_client:
        series = create_series(source_client)
        task = wait_for_task(
            source_client,
            series["initializationTask"]["taskId"],
        )
        refreshed = source_client.get(
            f"/api/series/{series['id']}"
        ).json()
        first_section_id = refreshed["books"][0]["chapters"][0][
            "sections"
        ][0]["id"]
        generated = source_client.get(
            f"/api/sections/{first_section_id}"
        )

    assert task["status"] == "succeeded"
    assert generated.status_code == 200
    assert ai.repair_requests == []
    body = generated.json()
    assert body["content"]["sources"] == []
    assert body["content"]["sourceVerification"] == []


def test_model_only_route_does_not_turn_missing_sources_into_governance():
    ai = RetryBlacklistAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            MissingSourceVerifier(),
        ),
        raise_server_exceptions=False,
    ) as source_client:
        series = create_series(source_client)
        task = wait_for_task(
            source_client,
            series["initializationTask"]["taskId"],
        )
        refreshed = source_client.get(
            f"/api/series/{series['id']}"
        ).json()
        section_id = refreshed["books"][0]["chapters"][0]["sections"][
            0
        ]["id"]
        recovered = source_client.get(f"/api/sections/{section_id}")

    assert task["status"] == "succeeded"
    assert task["attemptCount"] == 1
    assert recovered.status_code == 200
    assert ai.content_requests
    assert all(item == {"urls": [], "hosts": []} for item in ai.content_requests)
    assert ai.quiz_requests
    assert all(item == {
        "unverifiedSourceIndexes": [],
        "contentReliability": None,
    } for item in ai.quiz_requests)
    body = recovered.json()
    assert body["content"]["confidence"] == "high"
    assert body["content"]["sources"] == []
    assert body["content"]["sourceVerification"] == []


def test_claim_verification_does_not_hold_the_sqlite_write_lock(tmp_path):
    database = tmp_path / "claim-verification-lock.db"
    verifier = WriteProbeVerifier(database)
    with TestClient(
        create_app(
            f"sqlite+pysqlite:///{database}",
            FakeAi(),
            verifier,
        )
    ) as source_client:
        series = create_series(source_client)
        task = wait_for_task(
            source_client,
            series["initializationTask"]["taskId"],
        )

        assert task["status"] == "succeeded"
        assert verifier.probed is True
        with source_client.app.state.sessions() as db:
            first_section_id = task["result"]["targetSectionId"]
            assert db.scalar(
                select(func.count()).select_from(ContentVersion).where(
                    ContentVersion.section_id == first_section_id,
                )
            ) == 1
            assert db.scalar(
                select(func.count()).select_from(QuizSet).where(
                    QuizSet.section_id == first_section_id,
                )
            ) == 1


def test_claim_verification_failure_publishes_no_content_or_quiz(tmp_path):
    database = tmp_path / "claim-verification-failure.db"
    with TestClient(
        create_app(
            f"sqlite+pysqlite:///{database}",
            FakeAi(),
            FailingClaimVerifier(),
        ),
        raise_server_exceptions=False,
    ) as source_client:
        series = create_series(source_client)
        task = wait_for_task(
            source_client,
            series["initializationTask"]["taskId"],
        )

        assert task["status"] == "failed"
        assert task["errorCode"] == "SOURCE_CLAIM_REVIEW_FAILED"
        with source_client.app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(ContentVersion)) == 0
            assert db.scalar(select(func.count()).select_from(QuizSet)) == 0


@pytest.fixture
def client(tmp_path):
    storage = LocalAttachmentStorage(tmp_path / "attachments")
    with TestClient(create_app("sqlite+pysqlite:///:memory:", FakeAi(), AcceptingSourceVerifier(), storage)) as value:
        yield value


def test_cached_next_section_becomes_ready_without_waiting_for_note(tmp_path):
    ai = ParallelWorkflowAi()
    storage = LocalAttachmentStorage(tmp_path / "parallel-attachments")
    database_url = f"sqlite+pysqlite:///{tmp_path / 'parallel.db'}"

    with TestClient(
        create_app(database_url, ai, AcceptingSourceVerifier(), storage)
    ) as parallel_client:
        series = create_series(parallel_client)
        initialization = wait_for_task(
            parallel_client,
            series["initializationTask"]["taskId"],
        )
        assert initialization["status"] == "succeeded"
        refreshed = parallel_client.get(
            f"/api/series/{series['id']}"
        ).json()
        first_section = refreshed["books"][0]["chapters"][0]["sections"][0]
        section = parallel_client.get(
            f"/api/sections/{first_section['id']}"
        ).json()
        with parallel_client.app.state.sessions() as db:
            lookahead = db.scalar(
                select(LearningTask).where(
                    LearningTask.task_type == "section_lookahead_preload",
                    LearningTask.section_id == first_section["id"],
                )
            )
            assert lookahead is not None
            lookahead_id = lookahead.id
        assert wait_for_task(
            parallel_client,
            lookahead_id,
        )["status"] == "succeeded"

        ai.parallel_phase = True
        result = parallel_client.post(
            f"/api/sections/{first_section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1], [1], [1], [1], [1]],
            },
        ).json()
        tasks = {task["type"]: task for task in result["workflowTasks"]}

        assert ai.note_started.wait(1), "note generation did not start"
        try:
            assert wait_for_task(
                parallel_client,
                tasks["next_section_preload"]["taskId"],
            )["status"] == "succeeded"
            note_task = parallel_client.get(
                f"/api/learning-tasks/{tasks['note_generation']['taskId']}"
            ).json()
            assert note_task["status"] == "running"
            ready_series = parallel_client.get(
                f"/api/series/{series['id']}"
            ).json()
            ready_sections = ready_series["books"][0]["chapters"][0]["sections"]
            assert ready_sections[0]["status"] == "completed"
            assert ready_sections[1]["status"] == "available"
            assert parallel_client.get(
                f"/api/sections/{ready_sections[1]['id']}"
            ).status_code == 200
        finally:
            ai.release_note.set()

        assert wait_for_task(
            parallel_client,
            tasks["note_generation"]["taskId"],
        )["status"] == "succeeded"
        assert ai.events == ["note_start", "note_end"]

        ready_series = parallel_client.get(
            f"/api/series/{series['id']}"
        ).json()
        ready_section = ready_series["books"][0]["chapters"][0]["sections"][1]
        assert ready_section["status"] == "available"
        assert parallel_client.get(
            f"/api/sections/{ready_section['id']}"
        ).status_code == 200


def test_complete_real_shape_vertical_slice(client):
    plan = client.post("/api/plans", json={"shelfId":"shelf_technology","topic":"Kubernetes","role":"技术人员","experience":"会 Docker","purpose":"参与部署排障","depth":"deep","details":"理解机制"})
    assert plan.status_code == 201
    series = plan.json(); assert len(series["books"]) == 2 and series["books"][1]["status"] == "locked"
    chapter_id = series["books"][0]["chapters"][0]["id"]
    chapter = client.post(f"/api/chapters/{chapter_id}/generate").json()
    section_id = chapter["sections"][0]["id"]
    section = client.post(f"/api/sections/{section_id}/generate").json()
    assert section["latestAttemptReview"] is None
    assert len(section["content"]["blocks"]) == 5
    assert all(block["id"].startswith(f"block_{section['content']['id']}_") for block in section["content"]["blocks"])
    assert all(item["reachable"] for item in section["content"]["sourceVerification"])
    assert all(question["selectionMode"] == "single" for question in section["quiz"]["questions"])
    assert all("correct" not in question for question in section["quiz"]["questions"])
    assert all("explanation" not in question for question in section["quiz"]["questions"])
    quiz_id = section["quiz"]["id"]
    failed = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":quiz_id,"answers":[[0],[0],[0],[1],[1]]}).json()
    assert failed["passed"] is False
    assert failed["results"][0] == {
        "correct": False,
        "explanation": "因为 B1",
        "objective": "目标1",
        "selectedOptions": [0],
        "correctOptions": [1],
        "missedOptions": [1],
        "incorrectOptions": [0],
    }
    assert failed["workflowTasks"][0]["type"] == "remediation_generation"
    assert failed["workflowTasks"][0]["status"] == "pending"
    assert failed["workflowTasks"][0]["triggerId"] == failed["attemptId"]
    remediation_task = next(
        task
        for task in failed["workflowTasks"]
        if task["type"] == "remediation_generation"
    )
    remediation_result = wait_for_task(client, remediation_task["taskId"])
    assert remediation_result["status"] == "succeeded", remediation_result
    remediated = client.get(f"/api/sections/{section_id}").json()
    assert remediated["quiz"]["generation"] == 2
    assert remediated["remediations"][-1]["blocks"]
    assert remediated["remediations"][-1]["sourceLineage"] == {
        "mode": "generation_trace",
        "generationRunId": remediated["generation"]["id"],
    }
    assert remediated["remediations"][-1]["sourceVerification"] == [
        {
            "url": "https://kubernetes.io/docs/",
            "reachable": True,
                "statusCode": 200,
                "pinned": True,
                "verificationStatus": "verified",
        }
    ]
    with client.app.state.sessions() as db:
        item_versions = db.scalars(
            select(AssessmentItemVersion)
            .where(
                AssessmentItemVersion.quiz_set_id == remediated["quiz"]["id"]
            )
            .order_by(AssessmentItemVersion.position)
        ).all()
        assert len(item_versions) == len(remediated["quiz"]["questions"])
        assert all(item.item_key for item in item_versions)
        evidence_bindings = db.scalars(
            select(AssessmentItemEvidenceBlock).where(
                AssessmentItemEvidenceBlock.assessment_item_version_id.in_(
                    [item.id for item in item_versions]
                )
            )
        ).all()
        assert len(evidence_bindings) == len(item_versions)

        # QuizSet JSON is a compatibility projection. Scoring must use the
        # immutable item versions even if that projection is corrupted.
        quiz_projection = db.get(QuizSet, remediated["quiz"]["id"])
        projected_questions = json.loads(quiz_projection.questions_json)
        for question in projected_questions:
            question["correct"] = [0]
        quiz_projection.questions_json = json.dumps(
            projected_questions,
            ensure_ascii=False,
        )
        db.commit()
    stale = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":quiz_id,"answers":[[1],[1],[1],[1],[1]]})
    assert stale.status_code == 409 and stale.json()["code"] == "QUIZ_STALE"
    passed = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":remediated["quiz"]["id"],"answers":[[1],[0],[1],[1],[1]]}).json()
    assert passed["passed"] is True
    assert passed["score"] == 4
    for task in passed["workflowTasks"]:
        assert wait_for_task(client, task["taskId"])["status"] == "succeeded"
    completed = client.get(f"/api/sections/{section_id}").json()
    assert completed["note"]
    assert completed["latestAttemptReview"]["attemptId"] == passed["attemptId"]
    assert completed["latestAttemptReview"]["passed"] is True
    assert completed["latestAttemptReview"]["score"] == 4
    assert len(completed["latestAttemptReview"]["questions"]) == 5
    assert all(
        "correct" not in question and "explanation" not in question
        for question in completed["latestAttemptReview"]["questions"]
    )
    assert completed["note"]["aiContent"]["personal_gaps"] == ["目标1"]
    refreshed_series = client.get(f"/api/series/{series['id']}").json()
    refreshed_sections = refreshed_series["books"][0]["chapters"][0]["sections"]
    assert refreshed_sections[0]["status"] == "completed"
    assert refreshed_sections[1]["status"] == "available"
    next_section = client.get(
        f"/api/sections/{refreshed_sections[1]['id']}"
    ).json()
    assert next_section["content"] is not None
    assert client.get("/api/learning-memory?shelf_id=shelf_technology").json()


def test_layered_note_preserves_history_and_reads_live_annotations(client):
    series = create_series(client)
    assert wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )["status"] == "succeeded"
    refreshed = client.get(f"/api/series/{series['id']}").json()
    section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
    section = client.get(f"/api/sections/{section_id}").json()
    if not section["content"]:
        section = client.post(f"/api/sections/{section_id}/generate").json()
    passed = client.post(
        f"/api/sections/{section_id}/quiz",
        json={
            "quizSetId": section["quiz"]["id"],
            "answers": [[1], [1], [1], [1], [1]],
        },
    ).json()
    assert passed["passed"] is True
    for task in passed["workflowTasks"]:
        assert wait_for_task(client, task["taskId"])["status"] == "succeeded"

    original = client.get(f"/api/sections/{section_id}").json()["note"]
    summary = original["layers"]["learningSummary"]
    assert summary["sourceContentVersionId"] == section["content"]["id"]
    assert summary["sourceContractVersion"] == "generated_note_v1"
    assert summary["sourceObservationWatermark"] > 0
    assert original["layers"]["reviewSupplements"] == []
    assert original["layers"]["userRevision"] is None

    invalid = client.post(
        f"/api/sections/{section_id}/note/review-supplements",
        json={
            "reviewEpisodeId": "review:missing-episode",
            "content": {"core_mechanism": ["不应写入"]},
        },
    )
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "NOTE_REVIEW_EPISODE_INVALID"

    first_revision = {**summary["content"], "boundaries": ["用户写的边界"]}
    saved = client.patch(
        f"/api/sections/{section_id}/note",
        json={"content": first_revision},
    ).json()
    second_revision = {**first_revision, "unresolved": ["用户保留的问题"]}
    saved_again = client.patch(
        f"/api/sections/{section_id}/note",
        json={"content": second_revision},
    ).json()
    assert saved["layers"]["userRevision"]["version"] == 1
    assert saved_again["layers"]["userRevision"]["version"] == 2
    assert saved_again["layers"]["learningSummary"]["content"] == summary["content"]
    assert saved_again["aiContent"] == summary["content"]

    review_episode_id = "review:episode-0001"
    with client.app.state.sessions() as db:
        observations = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.section_id == section_id,
                AssessmentObservation.user_id == "user_demo",
            )
        ).all()
        observation_count = len(observations)
        assert observation_count > 0
        for observation in observations:
            observation.assistance_mode = "unassisted_review"
            observation.learning_episode_id = review_episode_id
        db.commit()

    supplement_content = {"core_mechanism": ["复习后看清了一个新边界"]}
    supplemented = client.post(
        f"/api/sections/{section_id}/note/review-supplements",
        json={
            "reviewEpisodeId": review_episode_id,
            "content": supplement_content,
        },
    )
    assert supplemented.status_code == 201
    assert supplemented.json()["layers"]["reviewSupplements"][0]["content"] == supplement_content

    idempotent = client.post(
        f"/api/sections/{section_id}/note/review-supplements",
        json={
            "reviewEpisodeId": review_episode_id,
            "content": supplement_content,
        },
    )
    assert idempotent.status_code == 201
    assert len(idempotent.json()["layers"]["reviewSupplements"]) == 1
    conflict = client.post(
        f"/api/sections/{section_id}/note/review-supplements",
        json={
            "reviewEpisodeId": review_episode_id,
            "content": {"core_mechanism": ["冲突内容"]},
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "NOTE_REVIEW_EPISODE_REUSED"

    before_projection_change = client.get(
        f"/api/sections/{section_id}"
    ).json()["note"]
    frozen_layers = before_projection_change["layers"]
    with client.app.state.sessions() as db:
        projection = db.scalar(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.user_id == "user_demo"
            )
        )
        assert projection is not None
        projection.claim_status = "invalidated_for_test"
        projection.p_known_ppm = 1
        db.commit()
    after_projection_change = client.get(
        f"/api/sections/{section_id}"
    ).json()["note"]
    assert after_projection_change["layers"] == frozen_layers
    assert any(
        item["claimStatus"] == "invalidated_for_test"
        for item in after_projection_change["verificationAnnotations"]
    )

    with client.app.state.sessions() as db:
        note = db.scalar(
            select(LearningNote).where(LearningNote.section_id == section_id)
        )
        assert len(db.scalars(select(LearningNoteSummary).where(LearningNoteSummary.note_id == note.id)).all()) == 1
        assert len(db.scalars(select(LearningNoteReviewSupplement).where(LearningNoteReviewSupplement.note_id == note.id)).all()) == 1
        assert len(db.scalars(select(LearningNoteUserRevision).where(LearningNoteUserRevision.note_id == note.id)).all()) == 2
        assert len(db.scalars(select(AssessmentObservation).where(AssessmentObservation.section_id == section_id)).all()) == observation_count


def test_legacy_four_of_five_attempt_can_be_reassessed(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = client.post(
        f"/api/sections/{chapter['sections'][0]['id']}/generate"
    ).json()
    passed_results = [
        {
            "correct": index >= 1,
            "explanation": f"解析{index}",
            "objective": f"目标{index}",
            "selectedOptions": [0 if index < 1 else 1],
            "correctOptions": [1],
            "missedOptions": [1] if index < 1 else [],
            "incorrectOptions": [0] if index < 1 else [],
        }
        for index in range(5)
    ]
    failed_results = [
        {**item, "correct": index >= 2}
        for index, item in enumerate(passed_results)
    ]
    with client.app.state.sessions() as db:
        learning_run = db.scalar(
            select(LearningRun).where(LearningRun.series_id == series["id"])
        )
        db.add_all([
            QuizAttempt(
                id="attempt_legacy_four_of_five",
                quiz_set_id=section["quiz"]["id"],
                learning_run_id=learning_run.id,
                user_id=learning_run.user_id,
                request_hash="legacy-four-of-five",
                answers_json=json.dumps([[0], [1], [1], [1], [1]]),
                results_json=json.dumps(passed_results, ensure_ascii=False),
                passed=False,
                workflow_status="completed",
            ),
            QuizAttempt(
                id="attempt_legacy_three_of_five",
                quiz_set_id=section["quiz"]["id"],
                learning_run_id=learning_run.id,
                user_id=learning_run.user_id,
                request_hash="legacy-three-of-five",
                answers_json=json.dumps([[0], [0], [1], [1], [1]]),
                results_json=json.dumps(failed_results, ensure_ascii=False),
                passed=False,
                workflow_status="completed",
            ),
        ])
        db.commit()

    promoted = client.post(
        f"/api/sections/{section['id']}/quiz-attempts/attempt_legacy_four_of_five/reassess"
    )
    assert promoted.status_code == 200
    assert promoted.json()["passed"] is True
    assert promoted.json()["score"] == 4
    assert {
        task["type"] for task in promoted.json()["workflowTasks"]
    } == {"note_generation", "next_section_preload"}
    assert all(
        task["triggerId"] == "attempt_legacy_four_of_five"
        for task in promoted.json()["workflowTasks"]
    )
    replayed = client.post(
        f"/api/sections/{section['id']}/quiz-attempts/attempt_legacy_four_of_five/reassess"
    )
    assert replayed.status_code == 200
    with client.app.state.sessions() as db:
        snapshots = db.scalars(
            select(LearningDecisionSnapshot)
            .where(
                LearningDecisionSnapshot.attempt_id
                == "attempt_legacy_four_of_five"
            )
            .order_by(LearningDecisionSnapshot.decision_kind)
        ).all()
        assert [item.decision_kind for item in snapshots] == [
            "assessment_gate",
            "progression",
        ]
        gate_snapshot = snapshots[0]
        assert gate_snapshot.rule_version == "legacy_score_gate_v1"
        assert gate_snapshot.trigger_kind == "quiz_reassess"
        assert gate_snapshot.source_observation_watermark == 0
        assert json.loads(gate_snapshot.input_snapshot_json)[
            "decisionBasis"
        ] == "legacy_score_reassessment"
        assert json.loads(gate_snapshot.output_decision_json)["score"] == {
            "adjusted": 4,
            "fixedTotal": 5,
            "initial": 4,
        }

    rejected = client.post(
        f"/api/sections/{section['id']}/quiz-attempts/attempt_legacy_three_of_five/reassess"
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "QUIZ_PASS_THRESHOLD_NOT_MET"
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(func.count(LearningDecisionSnapshot.id)).where(
                LearningDecisionSnapshot.attempt_id
                == "attempt_legacy_three_of_five"
            )
        ) == 0


def test_staged_generation_verifies_sources_before_generating_quiz(tmp_path):
    events = []
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        StagedFakeAi(events),
        RecordingVerifier(events),
        LocalAttachmentStorage(tmp_path / "attachments"),
    )
    with TestClient(app) as staged_client:
        plan = staged_client.post("/api/plans", json={
            "shelfId": "shelf_technology",
            "topic": "Staged generation",
            "role": "技术人员",
            "experience": "会 Docker",
            "purpose": "验证调用顺序",
            "depth": "deep",
        }).json()
        chapter_id = plan["books"][0]["chapters"][0]["id"]
        chapter = staged_client.post(f"/api/chapters/{chapter_id}/generate").json()
        section_id = chapter["sections"][0]["id"]
        response = staged_client.post(f"/api/sections/{section_id}/generate")

    assert response.status_code == 200
    assert events[-3:] == ["content", "verify", "quiz"]


def test_source_repair_scope_discards_unaffected_block_rewrite():
    source_a = Source(title="A", url="https://example.com/a", kind="official", version="1")
    source_b = Source(title="B", url="https://example.com/b", kind="official", version="1")
    before = GeneratedContent(
        confidence="high",
        sources=[source_a, source_b],
        blocks=[
            ContentBlock(kind="text", role=role, heading=role, content=f"原文 {index}。", source_indexes=[index % 2])
            for index, role in enumerate(["conclusion", "mechanism", "example", "boundary", "practice"])
        ],
    )
    after = before.model_copy(deep=True)
    after.sources[1] = Source(title="B2", url="https://example.com/b2", kind="official", version="2")
    after.blocks[0].content = "不应被来源修复改写"

    merged = apply_source_repair_scope(
        before,
        after,
        [{"url": "https://example.com/b"}],
    )

    assert merged.sources[0] == before.sources[0]
    assert merged.sources[1] == after.sources[1]
    assert merged.blocks[0] == before.blocks[0]


def test_semantic_evidence_scopes_remediation_generation_to_remediation():
    generation = {
        "id": "generation_1",
        "operation": "remediation",
        "trace": {
            "quizSetId": "quiz_2",
            "sourceVerification": [{"url": "https://example.test/source"}],
        },
    }
    learner = {
        "featureEvidence": {"secondBookGeneratedSectionId": None},
        "steps": [
            {
                "name": "GET /api/sections/section_1",
                "status": "PASS",
                "evidence": {
                    "payload": {
                        "id": "section_1",
                        "content": {"id": "content_1"},
                        "generation": generation,
                        "remediations": [
                            {"replacementQuizId": "quiz_2", "blocks": []}
                        ],
                    }
                },
            }
        ],
    }

    samples, _ = _semantic_evidence(learner)

    assert samples[0]["generation"] is None
    assert samples[0]["latestWorkflowGeneration"] == generation
    remediation = samples[0]["remediations"][0]
    assert remediation["generation"] == generation
    assert remediation["sourceLineage"]["generationRunId"] == "generation_1"


def test_durable_and_dual_user_gates_use_independent_test_results():
    success = SimpleNamespace(returncode=0, stdout="passed", stderr="")
    durable_failure = SimpleNamespace(
        returncode=1,
        stdout="durable failure",
        stderr="",
    )

    checks = _evaluation_checks(
        success,
        success,
        durable_failure,
        success,
    )

    assert checks["backendTests"] is True
    assert checks["durableTaskRecovery"] is False
    assert checks["dualUserIsolation"] is True
    assert checks["checkEvidence"]["durableTaskRecovery"]["returnCode"] == 1
    assert checks["checkEvidence"]["durableTaskRecovery"]["targets"]
    assert checks["checkEvidence"]["dualUserIsolation"]["targets"]


def test_semantic_review_rejects_modified_database_snapshot(
    tmp_path,
    monkeypatch,
):
    snapshot = tmp_path / "evaluation.db"
    snapshot.write_bytes(b"modified snapshot")
    monkeypatch.setattr("app.evaluation.runner.ROOT", tmp_path)
    report = {
        "evidenceSnapshot": {
            "database": {
                "path": snapshot.name,
                "sha256": hashlib.sha256(b"original snapshot").hexdigest(),
            }
        }
    }

    with pytest.raises(RuntimeError, match="snapshot hash mismatch"):
        _snapshot_database_facts(report)


def test_new_plan_preloads_first_lesson_in_durable_background_task(client):
    response = client.post(
        "/api/plans",
        json={
            "shelfId": "shelf_technology",
            "topic": "首节体验",
            "role": "技术人员",
            "experience": "会 Docker",
            "purpose": "立即开始学习",
            "depth": "deep",
        },
    )
    assert response.status_code == 201
    series = response.json()
    task = series["initializationTask"]
    assert task["type"] == "initial_book_preload"
    assert task["sectionId"] is None
    completed = wait_for_task(client, task["taskId"])
    assert completed["status"] == "succeeded"

    refreshed = client.get(f"/api/series/{series['id']}").json()
    first_chapter = refreshed["books"][0]["chapters"][0]
    assert first_chapter["generated"] is True
    first_section = client.get(
        f"/api/sections/{first_chapter['sections'][0]['id']}"
    ).json()
    assert first_section["content"] is not None
    assert first_section["quiz"] is not None
    assert completed["result"]["targetSectionId"] == first_section["id"]


def test_quiz_exposes_selection_mode_without_leaking_answers(tmp_path):
    class MixedChoiceAi(FakeAi):
        async def lesson(self, request, memory, prior_questions=None):
            lesson = await super().lesson(request, memory, prior_questions)
            first = lesson.questions[0]
            lesson.questions[0] = first.model_copy(update={"correct": [0, 1]})
            return lesson

    storage = LocalAttachmentStorage(tmp_path / "mixed-choice-attachments")
    with TestClient(create_app("sqlite+pysqlite:///:memory:", MixedChoiceAi(), AcceptingSourceVerifier(), storage)) as mixed:
        series = create_series(mixed)
        chapter = mixed.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section = mixed.post(f"/api/sections/{chapter['sections'][0]['id']}/generate").json()
        questions = section["quiz"]["questions"]
        assert [question["selectionMode"] for question in questions] == ["multiple", "single", "single", "single", "single"]
        assert all("correct" not in question for question in questions)
        assert all("explanation" not in question for question in questions)
        answers = [[0, 1], [1], [1], [1], [1]]
        result = mixed.post(
            f"/api/sections/{section['id']}/quiz",
            json={"quizSetId": section["quiz"]["id"], "answers": answers},
        )
        assert result.status_code == 200 and result.json()["perfect"] is True


def test_quiz_submission_is_idempotent_and_does_not_duplicate_evidence(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = client.post(
        f"/api/sections/{chapter['sections'][0]['id']}/generate"
    ).json()
    body = {
        "quizSetId": section["quiz"]["id"],
        "answers": [[1] for _ in section["quiz"]["questions"]],
    }
    headers = {"Idempotency-Key": "quiz-submit-idempotency-1"}

    first = client.post(
        f"/api/sections/{section['id']}/quiz",
        json=body,
        headers=headers,
    )
    replay = client.post(
        f"/api/sections/{section['id']}/quiz",
        json=body,
        headers=headers,
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json()["attemptId"] == first.json()["attemptId"]
    assert replay.json()["knowledgeSettlement"] == first.json()["knowledgeSettlement"]
    settlement_updates = first.json()["knowledgeSettlement"]["updates"]
    assert len(settlement_updates) == 1
    assert settlement_updates[0]["change"] == "rank_up"
    assert settlement_updates[0]["before"]["rank"] == "unranked"
    assert settlement_updates[0]["after"]["rank"] == "bronze"
    assert first.json()["knowledgeSettlement"]["settlementId"]
    with client.app.state.sessions() as db:
        attempts = db.scalars(
            select(QuizAttempt).where(
                QuizAttempt.idempotency_key == "quiz-submit-idempotency-1"
            )
        ).all()
        evidence = db.scalars(
            select(LearningEvidence).where(
                LearningEvidence.section_id == section["id"],
                LearningEvidence.evidence_type == "quiz",
            )
        ).all()
        scoring = db.scalars(
            select(ScoringResult).where(ScoringResult.attempt_id == attempts[0].id)
        ).all()
        observations = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.attempt_id == attempts[0].id
            )
        ).all()
        qualification = db.scalars(
            select(EvidenceQualificationEvent).where(
                EvidenceQualificationEvent.observation_id.in_(
                    [item.id for item in observations]
                )
            )
        ).all()
        decisions = db.scalars(
            select(LearningDecisionSnapshot)
            .where(LearningDecisionSnapshot.attempt_id == attempts[0].id)
            .order_by(LearningDecisionSnapshot.decision_kind)
        ).all()
        assert len(attempts) == 1
        assert len(evidence) == len(section["quiz"]["questions"])
        assert len(scoring) == 1
        assert len(observations) == len(section["quiz"]["questions"])
        assert len(qualification) == len(observations) * 4
        assert [item.decision_kind for item in decisions] == [
            "assessment_gate",
            "knowledge_settlement",
            "progression",
        ]
        gate = decisions[0]
        gate_input = json.loads(gate.input_snapshot_json)
        gate_output = json.loads(gate.output_decision_json)
        assert gate.rule_version == "gate_v2"
        assert gate.source_observation_watermark == max(
            item.sequence for item in observations
        )
        assert gate_input["requiredTargetIds"]
        assert gate_output["passed"] is True
        assert gate_output["unresolvedRequiredTargetIds"] == []
        settlement = decisions[1]
        assert settlement.rule_version == "knowledge_rank_v3"
        frozen_updates = json.loads(settlement.output_decision_json)["updates"]
        assert len(frozen_updates) == 1
        assert frozen_updates[0]["change"] == "rank_up"
        assert frozen_updates[0]["after"]["rank"] == "bronze"
        progression = decisions[2]
        assert progression.rule_version == "progression_v2_book_outline_gate"
        assert json.loads(progression.input_snapshot_json)["section_id"] == section["id"]
        assert json.loads(progression.output_decision_json)["completed_section_id"] == section["id"]

    conflict = client.post(
        f"/api/sections/{section['id']}/quiz",
        json={**body, "answers": [[0] for _ in section["quiz"]["questions"]]},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    with client.app.state.sessions() as db:
        assert len(db.scalars(
            select(LearningDecisionSnapshot).where(
                LearningDecisionSnapshot.attempt_id == first.json()["attemptId"]
            )
        ).all()) == 3


def test_assessment_gate_remediates_only_failed_target_and_rebuilds(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section_id = chapter["sections"][0]["id"]
    generated = client.get(f"/api/sections/{section_id}").json()
    with client.app.state.sessions() as db:
        stored = db.get(Section, section_id)
        stored.objectives_json = json.dumps(["核心目标", "辅助目标"], ensure_ascii=False)
        db.execute(
            delete(SectionAssessmentTarget).where(
                SectionAssessmentTarget.section_id == section_id
            )
        )
        db.commit()
    with client.app.state.sessions() as db:
        stored = db.get(Section, section_id)
        contract = ensure_learning_contract(db, stored)
        quiz = db.get(QuizSet, generated["quiz"]["id"])
        content = db.get(ContentVersion, quiz.content_version_id)
        quiz.learning_contract_version_id = contract.id
        content.learning_contract_version_id = contract.id
        binding = db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.section_id == section_id
            )
        )
        if binding:
            binding.learning_contract_version_id = contract.id
        questions = json.loads(quiz.questions_json)
        for index, question in enumerate(questions):
            question["objective"] = "核心目标" if index < 4 else "辅助目标"
            question.pop("assessmentTargetId", None)
            question.pop("equivalenceGroupId", None)
        bound_questions = bind_questions_to_targets(
            db,
            stored,
            questions,
            contract,
        )
        item_versions = db.scalars(
            select(AssessmentItemVersion)
            .where(AssessmentItemVersion.quiz_set_id == quiz.id)
            .order_by(AssessmentItemVersion.position)
        ).all()
        for item, question in zip(
            item_versions,
            bound_questions,
            strict=True,
        ):
            payload = json.loads(item.payload_json)
            payload.update(question)
            payload["id"] = item.id
            payload["itemKey"] = item.item_key
            item.assessment_target_id = question["assessmentTargetId"]
            item.payload_json = json.dumps(payload, ensure_ascii=False)
            question.update({
                "id": item.id,
                "itemKey": item.item_key,
                "evidenceBlockIds": payload.get("evidenceBlockIds", []),
            })
        quiz.questions_json = json.dumps(bound_questions, ensure_ascii=False)
        blocks = json.loads(content.blocks_json)
        for block in blocks:
            block["assessment_objectives"] = ["核心目标", "辅助目标"]
        content.blocks_json = json.dumps(blocks, ensure_ascii=False)
        reevaluate_generated_governance(
            db,
            quiz_id=quiz.id,
            actor_id="test_contract_rebind",
        )
        db.commit()

    section = client.get(f"/api/sections/{section_id}").json()
    original_targets = {
        question["objective"]: question["assessmentTargetId"]
        for question in section["quiz"]["questions"]
    }
    assert set(original_targets) == {"核心目标", "辅助目标"}
    assert all(
        question["core"] == (question["objective"] == "核心目标")
        for question in section["quiz"]["questions"]
    )

    answers = [[1] for _ in section["quiz"]["questions"]]
    for index, question in enumerate(section["quiz"]["questions"]):
        if question["objective"] == "核心目标":
            answers[index] = [0]
    failed = client.post(
        f"/api/sections/{section_id}/quiz",
        json={"quizSetId": section["quiz"]["id"], "answers": answers},
    ).json()
    assert failed["score"] == 1
    assert failed["passed"] is False
    remediation_task = next(
        task for task in failed["workflowTasks"]
        if task["type"] == "remediation_generation"
    )

    with client.app.state.sessions() as db:
        core_target_id = original_targets["核心目标"]
        p_known_after_failure = db.scalar(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.assessment_target_id == core_target_id
            )
        ).p_known_ppm

    remediation_result = wait_for_task(client, remediation_task["taskId"])
    assert remediation_result["status"] == "succeeded", remediation_result
    remediated = client.get(f"/api/sections/{section_id}").json()
    replacement = remediated["quiz"]
    with client.app.state.sessions() as db:
        stored_replacement = db.get(QuizSet, replacement["id"])
        replacement_questions = json.loads(stored_replacement.questions_json)
        source_items = db.scalars(
            select(AssessmentItemVersion)
            .where(
                AssessmentItemVersion.quiz_set_id == section["quiz"]["id"],
                AssessmentItemVersion.assessment_target_id == core_target_id,
            )
            .order_by(AssessmentItemVersion.position)
        ).all()
        source_evidence = {
            item.id: tuple(json.loads(item.payload_json)["evidenceBlockIds"])
            for item in source_items
        }
        assert all("claim_block_indexes" not in item for item in replacement_questions)
        assert {
            tuple(item["evidenceBlockIds"])
            for item in replacement_questions
        } == set(source_evidence.values())
        assert {
            item["sourceAssessmentItemVersionId"]
            for item in replacement_questions
        } == set(source_evidence)
    assert {
        question["assessmentTargetId"] for question in replacement["questions"]
    } == {original_targets["核心目标"]}
    assert original_targets["辅助目标"] not in {
        question["assessmentTargetId"] for question in replacement["questions"]
    }

    remediation_answers = [[1] for _ in replacement["questions"]]
    resolved = client.post(
        f"/api/sections/{section_id}/quiz",
        json={"quizSetId": replacement["id"], "answers": remediation_answers},
    ).json()
    assert resolved["passed"] is True

    memory = client.get(
        "/api/learning-memory?shelf_id=shelf_technology"
    ).json()
    core_memory = next(
        item for item in memory
        if item.get("assessmentTargetId") == original_targets["核心目标"]
    )
    assert core_memory["projectionRuleVersion"] == "mastery_v3"
    assert core_memory["pKnown"] == 0.056604
    assert core_memory["mastery"] == 6

    with client.app.state.sessions() as db:
        state = db.scalar(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.assessment_target_id == core_target_id
            )
        )
        gate = db.scalar(
            select(AssessmentGateState).where(
                AssessmentGateState.section_id == section_id,
                AssessmentGateState.assessment_target_id == core_target_id,
            )
        )
        assert p_known_after_failure == 38462
        # Initial and immediate remediation share one episode: replay applies
        # one assisted aggregate update, not two independent BKT updates.
        assert state.p_known_ppm == 56604
        assert gate.status == "resolved_remediation"
        expected = (
            state.p_known_ppm,
            state.retention_rounds,
            state.claim_status,
            gate.status,
        )
        state.p_known_ppm = 999999
        state.retention_rounds = 99
        state.claim_status = "corrupted"
        gate.status = "unresolved"
        db.commit()

        report = rebuild_user_projections(db, user_id=state.user_id)
        db.refresh(state)
        db.refresh(gate)
        assert report["assessment"]["observations"] == len(section["quiz"]["questions"]) + len(replacement["questions"])
        assert (
            state.p_known_ppm,
            state.retention_rounds,
            state.claim_status,
            gate.status,
        ) == expected


def test_retention_discounts_same_source_and_counts_delayed_novel_review(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = client.post(
        f"/api/sections/{chapter['sections'][0]['id']}/generate"
    ).json()
    answers = [[1] for _ in section["quiz"]["questions"]]
    initial = client.post(
        f"/api/sections/{section['id']}/quiz",
        json={"quizSetId": section["quiz"]["id"], "answers": answers},
    ).json()
    duplicate = client.post(
        f"/api/sections/{section['id']}/quiz",
        json={"quizSetId": section["quiz"]["id"], "answers": answers},
    ).json()

    with client.app.state.sessions() as db:
        initial_rows = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.attempt_id == initial["attemptId"]
            )
        ).all()
        duplicate_rows = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.attempt_id == duplicate["attemptId"]
            )
        ).all()
        assert {item.assistance_mode for item in duplicate_rows} == {
            "unassisted_repeat"
        }
        initial_at = min(item.created_at for item in initial_rows)
        for item in duplicate_rows:
            item.created_at = initial_at + timedelta(days=4)
        rebuild_assessment_projections(db, user_id=initial_rows[0].user_id)
        target_id = initial_rows[0].assessment_target_id
        state = db.scalar(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.user_id == initial_rows[0].user_id,
                KnowledgeStateProjection.assessment_target_id == target_id,
            )
        )
        assert state.retention_rounds == 0

        original_quiz = db.get(QuizSet, section["quiz"]["id"])
        novel_questions = json.loads(original_quiz.questions_json)
        for index, question in enumerate(novel_questions):
            question.pop("id", None)
            question.pop("itemKey", None)
            question["prompt"] = f"延迟复习变式 {index}"
            question["equivalenceGroupId"] = f"novel-review-{index}"
        novel_quiz = QuizSet(
            id="quiz_delayed_novel_review",
            section_id=section["id"],
            content_version_id=original_quiz.content_version_id,
            learning_contract_version_id=(
                original_quiz.learning_contract_version_id
            ),
            generation=original_quiz.generation + 1,
            questions_json=json.dumps(novel_questions, ensure_ascii=False),
        )
        db.add(novel_quiz)
        db.flush()
        publish_assessment_item_versions(
            db,
            quiz=novel_quiz,
            questions=novel_questions,
            evidence_block_ids_by_position=[
                question.get("evidenceBlockIds", [])
                for question in novel_questions
            ],
            uid=lambda prefix: f"{prefix}_{uuid4().hex}",
        )
        reevaluate_generated_governance(
            db,
            quiz_id=novel_quiz.id,
            actor_id="simulated_review_assignment",
        )
        db.add(
            Remediation(
                id="remediation_simulated_review_assignment",
                section_id=section["id"],
                attempt_id=initial["attemptId"],
                replacement_quiz_id=novel_quiz.id,
                blocks_json="[]",
                objectives_json="[]",
                strategy="simulated_review_assignment",
            )
        )
        db.commit()

    novel = client.post(
        f"/api/sections/{section['id']}/quiz",
        json={"quizSetId": "quiz_delayed_novel_review", "answers": answers},
    ).json()
    with client.app.state.sessions() as db:
        novel_rows = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.attempt_id == novel["attemptId"]
            )
        ).all()
        # Simulate the future ReviewAssignment authorization. A normal repeat
        # is deliberately never classified as delayed review by SubmitQuiz.
        for item in novel_rows:
            item.assistance_mode = "unassisted_review"
            item.learning_episode_id = "review:simulated-assignment"
            qualification = db.scalar(
                select(EvidenceQualificationEvent).where(
                    EvidenceQualificationEvent.observation_id == item.id,
                    EvidenceQualificationEvent.projection_family == "retention",
                    EvidenceQualificationEvent.rule_version == "evidence_v3",
                )
            )
            qualification.status = "candidate"
            qualification.reason = "simulated review assignment authorization"
        # This fact has a later sequence but an earlier event time than the
        # already-inserted duplicate review. Rebuild must replay event time.
        for item in novel_rows:
            item.created_at = initial_at + timedelta(days=2)
        rebuild_assessment_projections(db, user_id=novel_rows[0].user_id)
        state = db.scalar(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.user_id == novel_rows[0].user_id,
                KnowledgeStateProjection.assessment_target_id == target_id,
            )
        )
        assert state.retention_rounds == 1
        assert state.claim_status == "verified_delayed"


def test_due_review_api_enforces_daily_budget_without_creating_task_debt(client):
    series = create_series(client)
    assert wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )["status"] == "succeeded"
    series_view = client.get(f"/api/series/{series['id']}").json()
    section_id = series_view["books"][0]["chapters"][0]["sections"][0]["id"]
    current = now()
    with client.app.state.sessions() as db:
        content = db.scalar(
            select(ContentVersion)
            .where(ContentVersion.section_id == section_id)
            .order_by(ContentVersion.version.desc())
        )
        contract_id = content.learning_contract_version_id
        run = db.scalar(
            select(LearningRun).where(LearningRun.series_id == series["id"])
        )
        targets = []
        for index, priority in enumerate((20, 90, 50)):
            target = AssessmentTarget(
                id=f"target_budget_{index}",
                objective_key=f"budget-key-{index}",
                objective_statement=f"预算目标 {index}",
                dimension="recognition",
                target_depth="standard",
                status="active",
            )
            targets.append(target)
            db.add(target)
            db.flush()
            db.add(LearningContractAssessmentTarget(
                id=f"contract_target_budget_{index}",
                contract_version_id=contract_id,
                assessment_target_id=target.id,
                position=100 + index,
                required=False,
                verification_policy="choice_quiz_v1",
                evidence_policy="assessment_evidence_v1",
                diagnostic_only=False,
            ))
            question = {
                "prompt": f"预算目标 {index} 的延迟判断题",
                "options": ["错误 A", "正确 B", "错误 C"],
                "correct": [1],
                "core": False,
                "objective": target.objective_statement,
                "explanation": "B 与原教材机制一致。",
                "difficulty": "standard",
                "assessmentTargetId": target.id,
                "equivalenceGroupId": f"{target.id}:initial",
            }
            quiz = QuizSet(
                id=f"quiz_budget_{index}",
                section_id=section_id,
                content_version_id=content.id,
                learning_contract_version_id=contract_id,
                generation=100 + index,
                questions_json=json.dumps([question], ensure_ascii=False),
            )
            attempt = QuizAttempt(
                id=f"attempt_budget_{index}",
                quiz_set_id=quiz.id,
                learning_contract_version_id=contract_id,
                content_version_id=content.id,
                learning_run_id=run.id,
                user_id="user_demo",
                idempotency_key=f"budget-attempt-{index}",
                request_hash=hashlib.sha256(str(index).encode()).hexdigest(),
                answers_json="[[1]]",
                results_json=json.dumps([{
                    "correct": True,
                    "explanation": question["explanation"],
                    "objective": target.objective_statement,
                    "selectedOptions": [1],
                    "correctOptions": [1],
                    "missedOptions": [],
                    "incorrectOptions": [],
                }], ensure_ascii=False),
                passed=True,
                workflow_status="succeeded",
            )
            scoring = ScoringResult(
                id=f"scoring_budget_{index}",
                attempt_id=attempt.id,
                score=1,
                total=1,
                passed=True,
                results_json=attempt.results_json,
            )
            # These models deliberately expose no ORM relationships; flush the
            # immutable fact chain in foreign-key order.
            db.add(quiz)
            db.flush()
            db.add(attempt)
            db.flush()
            db.add(scoring)
            db.flush()
            db.add(AssessmentObservation(
                id=f"observation_budget_{index}",
                learning_run_id=run.id,
                user_id="user_demo",
                section_id=section_id,
                attempt_id=attempt.id,
                quiz_set_id=quiz.id,
                learning_contract_version_id=contract_id,
                content_version_id=content.id,
                scoring_result_id=scoring.id,
                assessment_target_id=target.id,
                question_index=0,
                correct=True,
                assistance_mode="unassisted_initial",
                learning_episode_id=f"quiz:{attempt.id}",
                equivalence_group_id=question["equivalenceGroupId"],
                qualification_at_creation="eligible_grouped",
                qualification_rule_version="evidence_v3",
                payload_json=json.dumps({
                    "questionFingerprint": hashlib.sha256(
                        json.dumps({
                            "prompt": question["prompt"],
                            "options": question["options"],
                            "correct": question["correct"],
                        }, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                }),
                created_at=current - timedelta(days=index + 2),
            ))
            db.add(ReviewState(
                id=f"review_budget_{index}",
                user_id="user_demo",
                assessment_target_id=target.id,
                status="scheduled",
                next_due_at=current - timedelta(days=index + 1),
                priority=priority,
                reason="retention_follow_up",
                spacing_stage=0,
            ))
        db.commit()

    queue = client.get("/api/reviews/due?daily_budget=2")
    assert queue.status_code == 200
    body = queue.json()
    assert body["dailyBudget"] == body["selectedCount"] == 2
    assert [item["assessmentTargetId"] for item in body["items"]] == [
        "target_budget_1",
        "target_budget_2",
    ]
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(ReviewState).where(ReviewState.id == "review_budget_0")
        ).status == "scheduled"
        assert db.scalar(
            select(LearningTask).where(
                LearningTask.task_type == "review"
            )
        ) is None


def test_concurrent_passing_submissions_trigger_first_completion_once(
    tmp_path,
):
    database = tmp_path / "concurrent.db"
    storage = LocalAttachmentStorage(tmp_path / "concurrent-attachments")
    with TestClient(
        create_app(
            f"sqlite+pysqlite:///{database}",
            FakeAi(),
            AcceptingSourceVerifier(),
            storage,
        )
    ) as concurrent_client:
        series = create_series(concurrent_client)
        chapter = concurrent_client.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = concurrent_client.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        body = {
            "quizSetId": section["quiz"]["id"],
            "answers": [[1] for _ in section["quiz"]["questions"]],
        }

        def submit(index):
            return concurrent_client.post(
                f"/api/sections/{section['id']}/quiz",
                json=body,
                headers={
                    "Idempotency-Key": f"concurrent-pass-request-{index}"
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(submit, [1, 2]))

        assert all(response.status_code == 200 for response in responses)
        with concurrent_client.app.state.sessions() as db:
            tasks = db.scalars(
                select(LearningTask).where(
                    LearningTask.section_id == section["id"],
                    LearningTask.task_type == "note_generation",
                )
            ).all()
            attempts = db.scalars(
                select(QuizAttempt).where(
                    QuizAttempt.learning_run_id == tasks[0].learning_run_id
                )
            ).all()
        assert len(tasks) == 1
        assert len(attempts) == 2


def test_note_failure_does_not_roll_back_pass_or_unlock(tmp_path):
    class FailingNoteAi(FakeAi):
        async def note(self, request):
            raise RuntimeError("simulated note failure")

    storage = LocalAttachmentStorage(tmp_path / "note-failure-attachments")
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            FailingNoteAi(),
            AcceptingSourceVerifier(),
            storage,
        )
    ) as failing:
        series = create_series(failing)
        chapter = failing.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = failing.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        response = failing.post(
            f"/api/sections/{section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1] for _ in section["quiz"]["questions"]],
            },
        )

        assert response.status_code == 200
        assert response.json()["passed"] is True
        note_generation = response.json()["noteGeneration"]
        failed_task = wait_for_task(failing, note_generation["taskId"])
        assert {
            key: failed_task[key]
            for key in ["status", "retryable", "errorCode"]
        } == {
            "status": "failed",
            "retryable": True,
            "errorCode": "RuntimeError",
        }
        assert failed_task["errorMessage"] == "后台任务执行失败，请安全重试"
        retried = failing.post(
            f"/api/note-tasks/{note_generation['taskId']}/retry"
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "pending"
        assert wait_for_task(
            failing,
            note_generation["taskId"],
        )["status"] == "failed"
        refreshed = failing.get(
            f"/api/series/{series['id']}"
        ).json()["books"][0]["chapters"][0]["sections"]
        assert refreshed[0]["status"] == "completed"
        assert refreshed[1]["status"] == "available"
        assert failing.get(f"/api/sections/{section['id']}").json()["note"] is None
        with failing.app.state.sessions() as db:
            task = db.scalar(
                select(LearningTask).where(
                    LearningTask.section_id == section["id"],
                    LearningTask.task_type == "note_generation",
                )
            )
            assert task.status == "failed"
            assert task.attempt_count == 2


def test_retryable_post_quiz_ai_failure_recovers_without_user_retry(tmp_path):
    class TransientNoteAi(FakeAi):
        def __init__(self):
            self.note_attempts = 0

        async def note(self, request):
            self.note_attempts += 1
            if self.note_attempts == 1:
                raise AiError("temporary provider failure")
            return await super().note(request)

    ai = TransientNoteAi()
    storage = LocalAttachmentStorage(tmp_path / "automatic-retry-attachments")
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
            storage,
        )
    ) as recovering:
        series = create_series(recovering)
        chapter = recovering.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = recovering.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        result = recovering.post(
            f"/api/sections/{section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1] for _ in section["quiz"]["questions"]],
            },
        ).json()

        note_task = next(
            task
            for task in result["workflowTasks"]
            if task["type"] == "note_generation"
        )
        completed = wait_for_task(recovering, note_task["taskId"])
        assert completed["status"] == "succeeded"
        assert completed["attemptCount"] == 2
        assert ai.note_attempts == 2
        assert recovering.get(
            f"/api/sections/{section['id']}"
        ).json()["note"]


def test_remediation_task_recovery_reuses_already_persisted_result(client):
    series = create_series(client)
    initialization = wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )
    section_id = initialization["result"]["targetSectionId"]
    section = client.get(f"/api/sections/{section_id}").json()
    failed = client.post(
        f"/api/sections/{section_id}/quiz",
        json={
            "quizSetId": section["quiz"]["id"],
            "answers": [[0] for _ in section["quiz"]["questions"]],
        },
    ).json()
    remediation_task = next(
        task
        for task in failed["workflowTasks"]
        if task["type"] == "remediation_generation"
    )
    first_completion = wait_for_task(client, remediation_task["taskId"])
    assert first_completion["status"] == "succeeded"
    persisted = client.get(f"/api/sections/{section_id}").json()
    assert len(persisted["remediations"]) == 1
    persisted_result = first_completion["result"]

    # Recreate the durable crash window: domain rows committed, task completion
    # missing. Recovery must reuse the existing remediation instead of calling
    # the model and inserting a second row for the same quiz attempt.
    with client.app.state.sessions() as db:
        task = db.get(LearningTask, remediation_task["taskId"])
        task.status = "pending"
        task.result_json = "{}"
        task.lease_owner = None
        task.lease_token = None
        task.lease_expires_at = None
        task.heartbeat_at = None
        db.commit()

    recovered = wait_for_task(
        client,
        remediation_task["taskId"],
        timeout=5,
    )
    assert recovered["status"] == "succeeded"
    assert recovered["attemptCount"] == 2
    assert recovered["result"] == persisted_result
    refreshed = client.get(f"/api/sections/{section_id}").json()
    assert len(refreshed["remediations"]) == 1
    assert refreshed["quiz"]["generation"] == 2


def test_local_maintenance_regenerates_remediation_as_a_new_revision(client):
    series = create_series(client)
    initialization = wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )
    section_id = initialization["result"]["targetSectionId"]
    section = client.get(f"/api/sections/{section_id}").json()
    failed = client.post(
        f"/api/sections/{section_id}/quiz",
        json={
            "quizSetId": section["quiz"]["id"],
            "answers": [[0] for _ in section["quiz"]["questions"]],
        },
    ).json()
    remediation_task = next(
        task
        for task in failed["workflowTasks"]
        if task["type"] == "remediation_generation"
    )
    assert wait_for_task(client, remediation_task["taskId"])["status"] == "succeeded"
    first = client.get(f"/api/sections/{section_id}").json()["remediations"][0]

    regenerated = client.post(
        f"/api/runtime/remediations/{first['id']}/regenerate"
    )

    assert regenerated.status_code == 200
    view = regenerated.json()
    assert len(view["remediations"]) == 1
    current = view["remediations"][0]
    assert current["id"] != first["id"]
    assert current["attemptId"] == first["attemptId"]
    assert view["quiz"]["generation"] == 3
    with client.app.state.sessions() as db:
        old = db.get(Remediation, first["id"])
        new = db.get(Remediation, current["id"])
        assert old is not None
        assert new.supersedes_id == old.id


def test_quiz_response_does_not_wait_for_post_quiz_ai(tmp_path):
    note_started = Event()
    release_note = Event()

    class SlowPostQuizAi(FakeAi):
        async def note(self, request):
            note_started.set()
            await asyncio.to_thread(release_note.wait)
            return await super().note(request)

    storage = LocalAttachmentStorage(tmp_path / "slow-task-attachments")
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            SlowPostQuizAi(),
            AcceptingSourceVerifier(),
            storage,
        )
    ) as non_blocking:
        series = create_series(non_blocking)
        chapter = non_blocking.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = non_blocking.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        with ThreadPoolExecutor(max_workers=1) as pool:
            response_future = pool.submit(
                non_blocking.post,
                f"/api/sections/{section['id']}/quiz",
                json={
                    "quizSetId": section["quiz"]["id"],
                    "answers": [[1] for _ in section["quiz"]["questions"]],
                },
            )
            try:
                response = response_future.result(timeout=5)
                assert note_started.wait(timeout=5)
                assert not release_note.is_set()
            finally:
                release_note.set()

        assert response.status_code == 200
        assert response.json()["passed"] is True
        assert {
            task["type"] for task in response.json()["workflowTasks"]
        } == {"note_generation", "next_section_preload"}
        for task in response.json()["workflowTasks"]:
            assert wait_for_task(
                non_blocking,
                task["taskId"],
            )["status"] == "succeeded"
        refreshed = non_blocking.get(
            f"/api/sections/{section['id']}"
        ).json()
        assert {
            task["type"] for task in refreshed["workflowTasks"]
        } == {"note_generation", "next_section_preload"}
        assert all(
            task["status"] == "succeeded"
            for task in refreshed["workflowTasks"]
        )


def test_interrupted_learning_task_resumes_after_restart(tmp_path):
    class InterruptedNoteAi(FakeAi):
        async def note(self, request):
            await asyncio.sleep(30)
            return await super().note(request)

    database = tmp_path / "task-restart.db"
    database_url = f"sqlite+pysqlite:///{database}"
    storage = LocalAttachmentStorage(tmp_path / "task-restart-attachments")
    with TestClient(
        create_app(
            database_url,
            InterruptedNoteAi(),
            AcceptingSourceVerifier(),
            storage,
        )
    ) as interrupted:
        series = create_series(interrupted)
        chapter = interrupted.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = interrupted.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        response = interrupted.post(
            f"/api/sections/{section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1] for _ in section["quiz"]["questions"]],
            },
        ).json()
        task_id = response["noteGeneration"]["taskId"]
        wait_for_task_status(interrupted, task_id, "running")

    with TestClient(
        create_app(
            database_url,
            FakeAi(),
            AcceptingSourceVerifier(),
            storage,
        )
    ) as restarted:
        assert wait_for_task(restarted, task_id)["status"] == "succeeded"


def test_locked_boundary(client):
    series = client.post("/api/plans", json={"shelfId":"shelf_technology","topic":"Kubernetes","role":"技术人员","experience":"会 Docker","depth":"deep"}).json()
    locked = series["books"][1]["chapters"][0]["id"]
    blocked = client.post(f"/api/chapters/{locked}/generate")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "BOOK_OUTLINE_CONFIRMATION_REQUIRED"


def test_completing_chapter_unlocks_first_pregenerated_section(client):
    series = create_series(client)
    first_chapter, next_chapter = series["books"][0]["chapters"]
    first = client.post(f"/api/chapters/{first_chapter['id']}/generate").json()
    with client.app.state.sessions() as db:
        db.add(
            Section(
                id="section_pregenerated_next",
                chapter_id=next_chapter["id"],
                position=1,
                title="预生成小节",
                question="预生成内容如何保持解锁语义？",
                objectives_json='["验证预生成章节解锁"]',
            )
        )
        learning_run = db.scalar(
            select(LearningRun).where(LearningRun.series_id == series["id"])
        )
        db.add(
            SectionProgress(
                id="section_progress_pregenerated_next",
                learning_run_id=learning_run.id,
                user_id=learning_run.user_id,
                section_id="section_pregenerated_next",
                status="locked",
            )
        )
        db.commit()

    for section in first["sections"]:
        generate_and_pass(client, section["id"])

    refreshed = client.get(f"/api/series/{series['id']}").json()
    unlocked_chapter = refreshed["books"][0]["chapters"][1]
    assert unlocked_chapter["status"] == "available"
    assert unlocked_chapter["sections"][0]["status"] == "available"


def test_series_soft_delete_hides_it_without_destroying_history(client):
    series = create_series(client)
    before = client.get("/api/bootstrap").json()
    assert any(item["id"] == series["id"] for item in before["shelves"][0]["series"])

    deleted = client.delete(f"/api/series/{series['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/series/{series['id']}").status_code == 404

    after = client.get("/api/bootstrap").json()
    assert all(item["id"] != series["id"] for item in after["shelves"][0]["series"])
    repeated = client.delete(f"/api/series/{series['id']}")
    assert repeated.status_code == 404
    assert repeated.json()["code"] == "SERIES_NOT_FOUND"


def test_deleted_ancestor_invalidates_all_descendant_routes(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section_id = chapter["sections"][0]["id"]
    assert client.delete(f"/api/series/{series['id']}").status_code == 204
    assert client.get(f"/api/sections/{section_id}").status_code == 404
    assert client.post(f"/api/sections/{section_id}/generate").status_code == 404
    assert client.get(f"/api/chapters/{chapter['id']}/practice").status_code == 404


def test_book_soft_delete_hides_book_and_preserves_audit_history(client):
    series = create_series(client)
    deleted_book = series["books"][1]

    deleted = client.delete(f"/api/books/{deleted_book['id']}")
    assert deleted.status_code == 204
    remaining = client.get(f"/api/series/{series['id']}").json()
    assert [item["id"] for item in remaining["books"]] == [series["books"][0]["id"]]
    assert client.get(f"/api/books/{deleted_book['id']}").status_code == 404
    repeated = client.delete(f"/api/books/{deleted_book['id']}")
    assert repeated.status_code == 404
    assert repeated.json()["code"] == "BOOK_NOT_FOUND"

    with client.app.state.sessions() as db:
        stored = db.get(Book, deleted_book["id"])
        revision = db.scalar(
            select(ChapterRevision).where(
                ChapterRevision.book_id == deleted_book["id"],
                ChapterRevision.action == "book_soft_delete",
            )
        )
        assert stored.deleted_at is not None
        assert revision is not None


def test_deleting_available_book_requires_next_outline_confirmation(client):
    series = create_series(client)
    first_book, second_book = series["books"]

    assert client.delete(f"/api/books/{first_book['id']}").status_code == 204
    remaining = client.get(f"/api/series/{series['id']}").json()["books"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == second_book["id"]
    assert remaining[0]["outlineStatus"] == "draft"
    assert remaining[0]["status"] == "locked"
    proposal = client.post(
        f"/api/books/{second_book['id']}/chapters/replan"
    ).json()
    confirmed = client.post(
        f"/api/books/{second_book['id']}/chapters/replan/"
        f"{proposal['proposalId']}/confirm"
    ).json()
    assert confirmed["status"] == "available"
    assert confirmed["chapters"][0]["status"] == "available"

    assert client.delete(f"/api/books/{second_book['id']}").status_code == 204
    assert client.get(f"/api/series/{series['id']}").status_code == 404
    bootstrap = client.get("/api/bootstrap").json()
    assert all(item["id"] != series["id"] for item in bootstrap["shelves"][0]["series"])
    with client.app.state.sessions() as db:
        stored_series = db.get(Series, series["id"])
        plan = db.get(LearningPlan, stored_series.plan_id)
        assert stored_series.deleted_at is not None
        assert plan.status == "deleted"


def create_series(client):
    return client.post("/api/plans", json={"shelfId":"shelf_technology","topic":"Kubernetes","role":"技术人员","experience":"会 Docker","depth":"deep"}).json()


def test_learning_start_preview_falls_back_and_direct_choice_is_auditable(client):
    body = {
        "shelfId": "shelf_technology",
        "topic": "Kubernetes",
        "role": "技术人员",
        "experience": "会 Docker",
        "purpose": "参与部署排障",
        "depth": "deep",
        "details": "理解机制",
    }

    preview = client.post("/api/learning-start/preview", json=body)
    assert preview.status_code == 201
    assert preview.json()["availability"] == "not_ready"
    assert preview.json()["nodes"] == []

    created = client.post(
        "/api/plans",
        json={**body, "startMode": "direct"},
        headers={"Idempotency-Key": "direct-learning-start-1"},
    )
    assert created.status_code == 201
    with client.app.state.sessions() as db:
        stored_preview = db.get(LearningStartPreview, preview.json()["previewId"])
        preference = db.scalar(
            select(SeriesLearningStartPreference).where(
                SeriesLearningStartPreference.series_id == created.json()["id"]
            )
        )
        assert stored_preview.user_id == "user_demo"
        assert preference.start_mode == "direct"
        assert json.loads(preference.selected_concept_revision_ids_json) == []


def test_chapter_skip_records_route_choice_without_fake_mastery(client):
    series = create_series(client)
    wait_for_task(client, series["initializationTask"]["taskId"])
    refreshed = client.get(f"/api/series/{series['id']}").json()
    chapter, next_chapter = refreshed["books"][0]["chapters"][:2]

    blocked = client.post(
        f"/api/chapters/{next_chapter['id']}/skip",
        json={"reason": "not_focus"},
        headers={"Idempotency-Key": "skip-locked-chapter-1"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "CHAPTER_LOCKED"

    with client.app.state.sessions() as db:
        evidence_before = db.scalar(
            select(func.count()).select_from(LearningEvidence)
        )

    skipped = client.post(
        f"/api/chapters/{chapter['id']}/skip",
        json={"reason": "not_focus"},
        headers={"Idempotency-Key": "skip-not-focus-chapter-1"},
    )
    assert skipped.status_code == 200
    assert skipped.json()["status"] == "skipped"
    assert skipped.json()["nextChapterId"] == next_chapter["id"]

    route = client.get(f"/api/series/{series['id']}").json()
    assert route["books"][0]["chapters"][0]["status"] == "skipped"
    assert route["books"][0]["chapters"][1]["status"] == "available"
    with client.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(LearningEvidence)) == evidence_before
        event = db.scalar(
            select(ChapterRouteDecisionEvent).where(
                ChapterRouteDecisionEvent.chapter_id == chapter["id"]
            )
        )
        assert event.reason == "not_focus"


def test_chapter_challenge_keeps_weak_sections_and_can_continue(client):
    series = create_series(client)
    wait_for_task(client, series["initializationTask"]["taskId"])
    refreshed = client.get(f"/api/series/{series['id']}").json()
    chapter, next_chapter = refreshed["books"][0]["chapters"][:2]

    prepared = client.post(
        f"/api/chapters/{chapter['id']}/challenge/prepare"
    )
    assert prepared.status_code == 200, prepared.json()
    challenge = prepared.json()
    assert len(challenge["sections"]) == 3
    assert all(
        "correct" not in question and "explanation" not in question
        for section in challenge["sections"]
        for question in section["questions"]
    )
    submissions = []
    for index, section in enumerate(challenge["sections"]):
        choice = 0 if index == 0 else 1
        submissions.append(
            {
                "sectionId": section["sectionId"],
                "quizSetId": section["quizSetId"],
                "answers": [[choice] for _ in section["questions"]],
            }
        )

    graded = client.post(
        f"/api/chapters/{chapter['id']}/challenge/submit",
        json={"sections": submissions},
        headers={"Idempotency-Key": "partial-chapter-challenge-1"},
    )
    assert graded.status_code == 200, graded.json()
    result = graded.json()
    assert result["passed"] is False
    assert [item["status"] for item in result["sectionResults"]] == [
        "needs_learning",
        "passed",
        "passed",
    ]
    route = client.get(f"/api/series/{series['id']}").json()
    assert route["books"][0]["chapters"][0]["status"] == "available"
    assert [
        item["status"]
        for item in route["books"][0]["chapters"][0]["sections"]
    ] == ["available", "completed", "completed"]

    continued = client.post(
        f"/api/chapters/{chapter['id']}/skip",
        json={"reason": "challenge_exit"},
        headers={"Idempotency-Key": "continue-after-challenge-1"},
    )
    assert continued.status_code == 200
    assert continued.json()["nextChapterId"] == next_chapter["id"]
    with client.app.state.sessions() as db:
        attempt = db.scalar(
            select(ChapterChallengeAttempt).where(
                ChapterChallengeAttempt.chapter_id == chapter["id"]
            )
        )
        assert attempt.passed is False
        facts = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.source_type == "chapter_challenge"
            )
        ).all()
        assert facts


def test_shelf_topics_are_rebuilt_from_confirmed_series(client):
    series = create_series(client)

    shelf = client.get("/api/bootstrap").json()["shelves"][0]
    assert shelf["domain"] == ""
    assert shelf["specialty"] == ""
    assert shelf["tags"] == ["Kubernetes"]

    assert client.delete(f"/api/series/{series['id']}").status_code == 204
    rebuilt = client.get("/api/bootstrap").json()["shelves"][0]
    assert rebuilt["tags"] == []


def test_plan_creation_persists_and_exposes_immutable_mission(client):
    series = create_series(client)

    response = client.get(f"/api/series/{series['id']}/mission")
    assert response.status_code == 200
    mission = response.json()

    assert mission["seriesId"] == series["id"]
    assert mission["version"] == 1
    assert mission["status"] == "confirmed"
    assert "Kubernetes" in mission["why"]
    assert mission["inferredFields"] == ["why"]
    assert mission["provenance"]["mode"] == "plan_creation"
    assert mission["adoption"]["source"] == "plan_creation"
    assert len(mission["successCriteria"]) == len(series["books"])
    assert all(
        item["acceptance"]["evidenceRule"] == "book_capstone_completed"
        for item in mission["successCriteria"]
    )

    with client.app.state.sessions() as db:
        stored_series = db.get(Series, series["id"])
        run = db.scalar(
            select(LearningRun).where(LearningRun.series_id == series["id"])
        )
        assert stored_series.initial_mission_version_id == mission["id"]
        assert run.initial_mission_version_id == mission["id"]


def test_mission_read_hides_unknown_or_unowned_series(client):
    response = client.get("/api/series/series_not_owned/mission")
    assert response.status_code == 404
    assert response.json()["code"] == "MISSION_NOT_FOUND"


def test_mission_revision_requires_confirmation_and_explicit_adoption(client):
    series = create_series(client)
    current = client.get(f"/api/series/{series['id']}/mission").json()
    payload = {
        "expectedCurrentMissionVersionId": current["id"],
        "why": "能够独立解释并排查 Kubernetes 工作负载异常",
        "targetCapabilities": current["targetCapabilities"],
        "constraints": current["constraints"],
        "outOfScope": ["集群供应商私有控制面实现"],
        "assumptions": current["assumptions"],
        "learnerContext": current["learnerContext"],
        "inferredFields": [],
        "successCriteria": [
            {
                "key": item["key"],
                "statement": item["statement"],
                "acceptance": item["acceptance"],
            }
            for item in current["successCriteria"]
        ],
    }
    draft_response = client.post(
        f"/api/series/{series['id']}/mission-versions", json=payload
    )
    assert draft_response.status_code == 201
    draft = draft_response.json()
    assert draft["status"] == "draft"
    assert client.get(f"/api/series/{series['id']}/mission").json()["id"] == current["id"]

    rejected = client.post(
        f"/api/series/{series['id']}/mission-adoptions",
        headers={"Idempotency-Key": "adopt-draft-001"},
        json={
            "missionVersionId": draft["id"],
            "expectedCurrentMissionVersionId": current["id"],
            "reason": "采用更明确的排障目标",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "MISSION_VERSION_NOT_CONFIRMED"

    confirmed = client.post(
        f"/api/series/{series['id']}/mission-versions/{draft['id']}/confirm"
    )
    assert confirmed.status_code == 200
    adopted = client.post(
        f"/api/series/{series['id']}/mission-adoptions",
        headers={"Idempotency-Key": "adopt-confirmed-001"},
        json={
            "missionVersionId": draft["id"],
            "expectedCurrentMissionVersionId": current["id"],
            "reason": "采用更明确的排障目标",
        },
    )
    assert adopted.status_code == 200
    assert adopted.json()["id"] == draft["id"]
    assert adopted.json()["adoption"]["source"] == "user"

    replay = client.post(
        f"/api/series/{series['id']}/mission-adoptions",
        headers={"Idempotency-Key": "adopt-confirmed-001"},
        json={
            "missionVersionId": draft["id"],
            "expectedCurrentMissionVersionId": current["id"],
            "reason": "采用更明确的排障目标",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == draft["id"]


def test_preload_creates_candidate_but_only_user_open_freezes_version(client):
    series = create_series(client)
    assert wait_for_task(
        client, series["initializationTask"]["taskId"]
    )["status"] == "succeeded"
    refreshed = client.get(f"/api/series/{series['id']}").json()
    section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]

    with client.app.state.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(LearningRunSectionBinding)
        ) == 0

    candidate = client.get(f"/api/sections/{section_id}").json()
    assert candidate["content"] is not None
    assert candidate["versionBinding"] is None

    opened = client.post(f"/api/sections/{section_id}/open")
    assert opened.status_code == 200
    binding = opened.json()["versionBinding"]
    assert binding["contentVersionId"] == candidate["content"]["id"]
    assert binding["initialQuizSetId"] == candidate["quiz"]["id"]

    reopened = client.post(f"/api/sections/{section_id}/open").json()
    assert reopened["versionBinding"]["id"] == binding["id"]
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(LearningRunSectionBinding)
        ) == 1


def test_milestone_path_is_proposed_confirmed_and_reconciled_with_goal_version(client):
    series = create_series(client)

    bootstrap = client.get("/api/bootstrap").json()
    dashboard = bootstrap["milestoneDashboard"]
    assert dashboard["goal"]["statement"]
    assert dashboard["path"]["seriesId"] == series["id"]
    assert dashboard["path"]["status"] == "proposed"
    assert dashboard["path"]["goalAligned"] is True
    assert 3 <= len(dashboard["path"]["milestones"]) <= 5
    assert dashboard["path"]["milestones"][0]["criteria"][0]["evidenceRule"] == "all_section_quizzes_passed"
    assert dashboard["today"]["seriesId"] == series["id"]

    confirmed = client.post(
        f"/api/series/{series['id']}/milestone-path/confirm"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    profile = bootstrap["profile"]
    corrected = client.put(
        "/api/profile",
        json={
            "profession": profile["profession"],
            "stage": profile["stage"],
            "purpose": "能够独立设计并解释一个可靠的 Kubernetes 平台",
            "domains": profile["domains"],
            "experience": profile["experience"],
            "weeklyMinutes": 300,
            "targetDate": "2027-01-31",
        },
    )
    assert corrected.status_code == 200
    assert corrected.json()["weeklyMinutes"] == 300

    stale = client.get("/api/bootstrap").json()["milestoneDashboard"]
    assert stale["goal"]["statement"].startswith("能够独立设计")
    assert stale["path"]["goalAligned"] is False

    reconfirmed = client.post(
        f"/api/series/{series['id']}/milestone-path/confirm"
    )
    assert reconfirmed.status_code == 200
    aligned = client.get("/api/bootstrap").json()["milestoneDashboard"]
    assert aligned["path"]["goalAligned"] is True

    with client.app.state.sessions() as db:
        path = db.scalar(
            select(MilestonePath).where(MilestonePath.series_id == series["id"])
        )
        revisions = db.scalars(
            select(MilestonePathRevision)
            .where(MilestonePathRevision.path_id == path.id)
            .order_by(MilestonePathRevision.version)
        ).all()
        assert [item.source for item in revisions] == [
            "ai_generation",
            "user_confirmation",
            "user_confirmation",
        ]
        assert [item.version for item in revisions] == [1, 2, 3]


def test_milestone_confirmation_rejects_unknown_series(client):
    response = client.post("/api/series/series_not_owned/milestone-path/confirm")
    assert response.status_code == 404
    assert response.json()["code"] == "MILESTONE_PATH_NOT_FOUND"


def wait_for_task(client, task_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/learning-tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in {"succeeded", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not finish")


def wait_for_task_status(client, task_id, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/learning-tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] == expected:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {expected}")


def sse_events(response):
    events = []
    for frame in response.text.replace("\r\n", "\n").split("\n\n"):
        if not frame.strip():
            continue
        event_type = "message"
        data_lines = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        if data_lines:
            events.append((event_type, json.loads("\n".join(data_lines))))
    return events


def test_plan_creation_is_idempotent(client):
    body = {"shelfId":"shelf_technology","topic":"并发创建保护","role":"技术人员","experience":"会 Docker","depth":"deep"}
    headers = {"Idempotency-Key": "test-plan-creation-idempotency"}
    first = client.post("/api/plans", json=body, headers=headers)
    replay = client.post("/api/plans", json=body, headers=headers)
    assert first.status_code == 201 and replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    conflict = client.post("/api/plans", json={**body, "topic": "不同主题"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_runtime_ai_settings_never_return_the_key_and_can_switch_to_demo(client):
    before = client.get("/api/runtime/ai")
    assert before.status_code == 200
    assert '"apiKey":' not in before.text
    assert before.json()["ephemeral"] is True
    assert before.json()["apiMode"] == "responses"
    assert before.json()["providerProtocol"] == "openai"
    assert before.json()["reasoningMode"] == "optional"

    switched = client.put(
        "/api/runtime/ai",
        json={"mode": "demo", "apiKey": "must-not-be-returned", "baseUrl": "", "model": "ignored"},
    )
    assert switched.status_code == 200
    assert switched.json()["mode"] == "demo"
    assert switched.json()["configured"] is False
    assert switched.json()["reasoningMode"] == "disabled"
    assert '"apiKey":' not in switched.text
    assert "must-not-be-returned" not in switched.text
    assert client.get("/api/health").json()["model"] == "local-demo-v1"


def test_runtime_ai_rejects_a_pool_without_an_active_route(client):
    response = client.put(
        "/api/runtime/ai",
        json={
            "mode": "provider",
            "apiKey": "test-provider-key",
            "baseUrl": "http://127.0.0.1:9999/v1",
            "model": "qwen3.8-max",
            "deployments": [
                {
                    "deploymentId": "disabled-author",
                    "providerId": "test-provider",
                    "model": "qwen3.8-max",
                    "modelFamilyId": "qwen",
                    "baseUrl": "http://127.0.0.1:9999/v1",
                    "structuredMode": "json_object",
                    "backendAllowed": True,
                    "allowedEnvironments": ["test"],
                    "status": "disabled",
                }
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "AI_RUNTIME_ROUTE_INVALID"


def test_library_read_model_has_fixed_query_budget_and_no_writes(client):
    series = create_series(client)
    first_chapter = series["books"][0]["chapters"][0]
    client.post(f"/api/chapters/{first_chapter['id']}/generate")
    engine = client.app.state.sessions.kw["bind"]
    statements = []

    def record_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = client.get(f"/api/series/{series['id']}")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    # The ninth fixed query is the authenticated user's required-profile gate.
    assert sum(item.startswith("SELECT") for item in statements) <= 9
    assert not any(
        item.startswith(("INSERT", "UPDATE", "DELETE"))
        for item in statements
    )


def test_missing_progress_projection_is_reported_without_read_repair(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section_id = chapter["sections"][0]["id"]
    with client.app.state.sessions() as db:
        db.execute(
            delete(SectionProgress).where(
                SectionProgress.section_id == section_id
            )
        )
        db.commit()

    response = client.get(f"/api/series/{series['id']}")
    assert response.status_code == 500
    assert response.json()["code"] == "SECTION_PROGRESS_MISSING"
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(SectionProgress).where(
                SectionProgress.section_id == section_id
            )
        ) is None


def test_runtime_ai_settings_reject_non_local_clients(tmp_path):
    storage = LocalAttachmentStorage(tmp_path / "remote-runtime-settings")
    with TestClient(
        create_app("sqlite+pysqlite:///:memory:", FakeAi(), AcceptingSourceVerifier(), storage),
        client=("203.0.113.10", 50000),
    ) as remote:
        denied = remote.get("/api/runtime/ai")
        assert denied.status_code == 403
        assert denied.json()["code"] == "AI_RUNTIME_LOCAL_ONLY"


def generate_and_pass(client, section_id):
    section = client.post(f"/api/sections/{section_id}/generate").json()
    answers = [[1] for _ in section["quiz"]["questions"]]
    result = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":section["quiz"]["id"],"answers":answers})
    assert result.status_code == 200 and result.json()["passed"]
    for task in result.json()["workflowTasks"]:
        assert wait_for_task(client, task["taskId"])["status"] == "succeeded"
    return section


def test_qa_correction_and_three_round_ask_me(client):
    series = create_series(client)
    chapter = client.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    block_id = section["content"]["blocks"][0]["id"]
    invalid_block = client.post(f"/api/sections/{section['id']}/ask", json={"blockId":"0","question":"旧数组下标锚点"})
    assert invalid_block.status_code == 409 and invalid_block.json()["code"] == "BLOCK_INVALID"
    with client.stream(
        "POST",
        f"/api/sections/{section['id']}/ask/stream",
        json={"blockId": block_id, "question": "请用 Markdown 列出机制和边界"},
    ) as streamed:
        events = [json.loads(line) for line in streamed.iter_lines() if line]
    assert streamed.status_code == 200
    assert events[0]["type"] == "delta" and events[-1]["type"] == "done"
    assert events[-1]["threadId"]
    first = client.post(f"/api/sections/{section['id']}/ask", json={"blockId":block_id,"question":"机制是什么？"}).json()
    second = client.post(f"/api/sections/{section['id']}/ask", json={"blockId":block_id,"question":"另一个问题","forceRelation":"new_question"}).json()
    corrected = client.patch(f"/api/sections/{section['id']}/qa/threads/{second['threadId']}", json={"relation":"follow_up","targetThreadId":first["threadId"]})
    assert corrected.status_code == 200 and corrected.json()["corrected"]
    started = client.post(f"/api/sections/{section['id']}/ask-me", json={"answer":""}).json()
    assert started["dimension"] == "mechanism"
    for expected, answer in [("boundary","机制回答"),("transfer","边界回答")]:
        value = client.post(f"/api/sections/{section['id']}/ask-me", json={"answer":answer}).json()
        assert value["dimension"] == expected
    finished = client.post(f"/api/sections/{section['id']}/ask-me", json={"answer":"迁移回答"}).json()
    assert finished["status"] == "completed" and [item["dimension"] for item in finished["entries"]] == ["mechanism","boundary","transfer"]
    before = client.get(f"/api/sections/{section['id']}").json()["note"]
    edited = client.patch(f"/api/sections/{section['id']}/note", json={"content":{"my":"补充"}}).json()
    assert edited["aiContent"] == before["aiContent"] and edited["userContent"] == {"my":"补充"}


def test_ask_ai_tracks_explicit_daily_mode_switch_but_not_expiry(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    block_id = section["content"]["blocks"][0]["id"]

    fast = client.put(
        "/api/daily-mode",
        headers={"Idempotency-Key": "qa-mode-fast"},
        json={
            "dailyMode": "fast",
            "duration": "1h",
            "timezone": "Asia/Shanghai",
            "source": "header_toggle",
        },
    )
    assert fast.status_code == 200
    assert client.post(
        f"/api/sections/{section['id']}/ask",
        json={"blockId": block_id, "question": "先说结论"},
    ).status_code == 200
    with client.app.state.sessions() as db:
        assert db.scalar(select(QaSession)).daily_mode == "fast"

    slow = client.put(
        "/api/daily-mode",
        headers={"Idempotency-Key": "qa-mode-slow"},
        json={
            "dailyMode": "slow",
            "duration": "1h",
            "timezone": "Asia/Shanghai",
            "source": "header_toggle",
        },
    )
    assert slow.status_code == 200
    assert client.post(
        f"/api/sections/{section['id']}/ask",
        json={"blockId": block_id, "question": "再说清楚机制"},
    ).status_code == 200
    with client.app.state.sessions() as db:
        assert db.scalar(select(QaSession)).daily_mode == "slow"
        state = db.scalar(select(UserDailyModeState))
        state.expires_at = now() - timedelta(seconds=1)
        db.commit()

    assert client.post(
        f"/api/sections/{section['id']}/ask",
        json={"blockId": block_id, "question": "到期后继续"},
    ).status_code == 200
    with client.app.state.sessions() as db:
        assert db.scalar(select(QaSession)).daily_mode == "slow"


def test_ask_me_retries_invalid_model_evaluation():
    class FlakyAskMeAi(FakeAi):
        answered_calls = 0

        async def ask_me(self, request):
            if request.get("previousAnswer"):
                self.answered_calls += 1
                if self.answered_calls == 1:
                    return AskMeTurn(dimension=request["dimension"], prompt="继续", evaluation="not_evaluated")
            return await super().ask_me(request)

    ai = FlakyAskMeAi()
    with TestClient(create_app("sqlite+pysqlite:///:memory:", ai, AcceptingSourceVerifier())) as retry_client:
        series = create_series(retry_client)
        chapter = retry_client.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section = generate_and_pass(retry_client, chapter["sections"][0]["id"])
        retry_client.post(f"/api/sections/{section['id']}/ask-me", json={"answer":""})
        advanced = retry_client.post(f"/api/sections/{section['id']}/ask-me", json={"answer":"机制回答"})
        assert advanced.status_code == 200 and advanced.json()["dimension"] == "boundary"
        assert ai.answered_calls == 2


def test_ask_me_discussion_is_resumable_and_turn_submissions_are_idempotent(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    path = f"/api/sections/{section['id']}/ask-me/discussion"

    assert client.get(path).json() is None
    started = client.post(path).json()
    assert started["status"] == "active"
    assert started["revision"] == 0
    assert len(started["topics"]) == 3
    assert [item["dimension"] for item in started["topics"]] == [
        "mechanism",
        "boundary",
        "transfer",
    ]

    turn_body = {
        "sessionId": started["id"],
        "topicId": started["activeTopicId"],
        "expectedRevision": 0,
        "answer": "我会先提出判断，再用一个可观察的业务信号验证它。",
    }
    headers = {"Idempotency-Key": "ask-me-v2-turn-0001"}
    submitted = client.post(f"{path}/turns", json=turn_body, headers=headers)
    assert submitted.status_code == 200
    first = submitted.json()
    assert first["revision"] == 1
    assert len(first["turns"]) == 1
    assert first["turns"][0]["feedback"]["correctPoints"]
    assert first["turns"][0]["feedback"]["issues"][0]["explanation"]
    assert first["turns"][0]["feedback"]["suggestions"]

    replay = client.post(f"{path}/turns", json=turn_body, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first
    assert len(client.get(path).json()["turns"]) == 1

    conflict_body = {**turn_body, "answer": "复用同一个键提交另一份答案"}
    conflict = client.post(
        f"{path}/turns",
        json=conflict_body,
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ASK_ME_DISCUSSION_IDEMPOTENCY_CONFLICT"

    stale = client.post(
        f"{path}/turns",
        json={**turn_body, "answer": "旧版本上的新回答"},
        headers={"Idempotency-Key": "ask-me-v2-turn-stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "ASK_ME_DISCUSSION_REVISION_CONFLICT"

    continued = client.post(
        f"{path}/turns",
        json={
            **turn_body,
            "expectedRevision": 1,
            "answer": "我继续沿着同一个主题补充反例和判断边界。",
        },
        headers={"Idempotency-Key": "ask-me-v2-turn-0002"},
    ).json()
    assert continued["activeTopicId"] == started["activeTopicId"]
    assert continued["topics"][0]["turnCount"] == 2


def test_ask_me_discussion_retry_survives_reload_with_a_new_key():
    class FailOnceDiscussionAi(FakeAi):
        attempts = 0

        async def ask_me_discussion(self, request):
            self.attempts += 1
            if self.attempts == 1:
                raise AiError("temporary Ask Me failure")
            return await super().ask_me_discussion(request)

    ai = FailOnceDiscussionAi()
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai,
            AcceptingSourceVerifier(),
        ),
        raise_server_exceptions=False,
    ) as retry_client:
        series = create_series(retry_client)
        chapter = retry_client.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = generate_and_pass(
            retry_client,
            chapter["sections"][0]["id"],
        )
        path = f"/api/sections/{section['id']}/ask-me/discussion"
        started = retry_client.post(path).json()
        body = {
            "sessionId": started["id"],
            "topicId": started["activeTopicId"],
            "expectedRevision": 0,
            "answer": "这份回答在网络失败后应以同一请求标识安全重试。",
        }
        headers = {"Idempotency-Key": "ask-me-v2-failed-retry"}

        failed = retry_client.post(f"{path}/turns", json=body, headers=headers)
        assert failed.status_code == 502
        assert retry_client.get(path).json()["pending"] is False

        recovered = retry_client.post(
            f"{path}/turns",
            json=body,
            headers={"Idempotency-Key": "ask-me-v2-failed-retry-after-reload"},
        )
        assert recovered.status_code == 200
        assert recovered.json()["revision"] == 1
        assert len(recovered.json()["turns"]) == 1
        assert ai.attempts == 2


def test_ask_me_discussion_reclaims_expired_processing_turn(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    path = f"/api/sections/{section['id']}/ask-me/discussion"
    started = client.post(path).json()
    body = {
        "sessionId": started["id"],
        "topicId": started["activeTopicId"],
        "expectedRevision": 0,
        "answer": "进程退出后，我仍应能够安全地重新提交这一轮回答。",
    }
    request_payload = {
        "sectionId": section["id"],
        "sessionId": started["id"],
        "topicId": started["activeTopicId"],
        "expectedRevision": 0,
        "answer": body["answer"],
    }
    request_hash = hashlib.sha256(json.dumps(
        request_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    turn_id = f"askme_turn_{uuid4().hex}"
    with client.app.state.sessions() as db:
        session = db.get(AskMeDiscussionSession, started["id"])
        topic = db.get(AskMeDiscussionTopic, started["activeTopicId"])
        db.add(AskMeDiscussionTurnRecord(
            id=turn_id,
            session_id=session.id,
            topic_id=topic.id,
            user_id=session.user_id,
            turn_index=0,
            prompt=topic.current_prompt,
            answer=body["answer"],
            evaluation="",
            feedback_json="{}",
            status="processing",
            idempotency_key="ask-me-v2-expired-turn",
            request_hash=request_hash,
            response_json="",
            error_code="",
            lease_token="expired-worker-token",
            lease_expires_at=now() - timedelta(seconds=1),
        ))
        session.pending_turn_id = turn_id
        db.commit()

    recovered = client.post(
        f"{path}/turns",
        json=body,
        headers={"Idempotency-Key": "ask-me-v2-expired-turn"},
    )
    assert recovered.status_code == 200
    assert recovered.json()["revision"] == 1
    assert recovered.json()["turns"][0]["id"] == turn_id
    with client.app.state.sessions() as db:
        turn = db.get(AskMeDiscussionTurnRecord, turn_id)
        assert turn.status == "completed"
        assert turn.lease_token == ""
        assert turn.lease_expires_at is None


def test_ask_me_discussion_user_controls_topic_switch_pause_and_finish(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    path = f"/api/sections/{section['id']}/ask-me/discussion"
    started = client.post(path).json()
    assert all(
        len(topic["assessmentTargetIds"]) == 1
        for topic in started["topics"]
    )
    answered = client.post(
        f"{path}/turns",
        json={
            "sessionId": started["id"],
            "topicId": started["activeTopicId"],
            "expectedRevision": 0,
            "answer": "这是一段包含判断依据和边界说明的自主回答。",
        },
        headers={"Idempotency-Key": "ask-me-v2-action-turn"},
    ).json()

    next_body = {
        "sessionId": started["id"],
        "expectedRevision": answered["revision"],
        "action": "next_topic",
    }
    action_headers = {"Idempotency-Key": "ask-me-v2-next-topic"}
    advanced = client.post(
        f"{path}/actions",
        json=next_body,
        headers=action_headers,
    )
    assert advanced.status_code == 200
    advanced_body = advanced.json()
    assert advanced_body["topics"][0]["status"] == "closed"
    assert advanced_body["topics"][0]["evidenceRecorded"]
    assert advanced_body["activeTopicId"] == advanced_body["topics"][1]["id"]
    assert client.post(
        f"{path}/actions",
        json=next_body,
        headers=action_headers,
    ).json() == advanced_body
    target_ids = set(started["topics"][0]["assessmentTargetIds"])
    with client.app.state.sessions() as db:
        oral_rows = db.scalars(
            select(AssessmentObservation).where(
                AssessmentObservation.section_id == section["id"],
                AssessmentObservation.source_type == "ask_me_topic",
            )
        ).all()
        assert {item.assessment_target_id for item in oral_rows} == target_ids
        assert all(item.attempt_id is None for item in oral_rows)
        assert all(item.scoring_result_id is None for item in oral_rows)
        gate_qualifications = db.scalars(
            select(EvidenceQualificationEvent).where(
                EvidenceQualificationEvent.observation_id.in_(
                    [item.id for item in oral_rows]
                ),
                EvidenceQualificationEvent.projection_family == "gate",
            )
        ).all()
        assert gate_qualifications
        assert all(item.status == "ineligible" for item in gate_qualifications)
        mastery_rows = db.scalars(
            select(KnowledgeStateProjection).where(
                KnowledgeStateProjection.assessment_target_id.in_(target_ids)
            )
        ).all()
        assert {item.assessment_target_id for item in mastery_rows} == target_ids
        oral_watermarks = {
            item.assessment_target_id: item.sequence for item in oral_rows
        }
        assert all(
            item.source_observation_watermark
            >= oral_watermarks[item.assessment_target_id]
            for item in mastery_rows
        )

    paused = client.post(
        f"{path}/actions",
        json={
            "sessionId": started["id"],
            "expectedRevision": advanced_body["revision"],
            "action": "pause",
        },
        headers={"Idempotency-Key": "ask-me-v2-pause"},
    ).json()
    assert paused["status"] == "paused"
    assert client.get(path).json()["status"] == "paused"

    resumed = client.post(
        f"{path}/actions",
        json={
            "sessionId": started["id"],
            "expectedRevision": paused["revision"],
            "action": "resume",
        },
        headers={"Idempotency-Key": "ask-me-v2-resume"},
    ).json()
    assert resumed["status"] == "active"

    finished = client.post(
        f"{path}/actions",
        json={
            "sessionId": started["id"],
            "expectedRevision": resumed["revision"],
            "action": "finish",
        },
        headers={"Idempotency-Key": "ask-me-v2-finish"},
    ).json()
    assert finished["status"] == "completed"
    assert len(finished["topics"]) == 3


def test_ask_me_discussion_rejects_multi_target_topic_evidence(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    path = f"/api/sections/{section['id']}/ask-me/discussion"
    started = client.post(path).json()
    answered = client.post(
        f"{path}/turns",
        json={
            "sessionId": started["id"],
            "topicId": started["activeTopicId"],
            "expectedRevision": 0,
            "answer": "我会说明机制、可观察依据，以及结论不成立的条件。",
        },
        headers={"Idempotency-Key": "ask-me-v2-target-boundary-turn"},
    ).json()
    with client.app.state.sessions() as db:
        topic = db.get(AskMeDiscussionTopic, started["activeTopicId"])
        valid_target_id = json.loads(topic.assessment_target_ids_json)[0]
        topic.assessment_target_ids_json = json.dumps([
            valid_target_id,
            "target_outside_frozen_contract",
        ])
        db.commit()

    rejected = client.post(
        f"{path}/actions",
        json={
            "sessionId": started["id"],
            "expectedRevision": answered["revision"],
            "action": "next_topic",
        },
        headers={"Idempotency-Key": "ask-me-v2-target-boundary-action"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "ASK_ME_EVIDENCE_TARGET_BOUNDARY_INVALID"
    with client.app.state.sessions() as db:
        topic = db.get(AskMeDiscussionTopic, started["activeTopicId"])
        oral_count = db.scalar(
            select(func.count(AssessmentObservation.id)).where(
                AssessmentObservation.section_id == section["id"],
                AssessmentObservation.source_type == "ask_me_topic",
            )
        )
        assert topic.status in {"active", "sufficient"}
        assert not topic.evidence_recorded
        assert oral_count == 0


def test_future_chapter_edits_and_started_boundary(client):
    series = create_series(client)
    draft_book = series["books"][1]
    assert draft_book["outlineStatus"] == "draft"
    early_activation = client.post(
        f"/api/books/{draft_book['id']}/chapters/replan"
    )
    assert early_activation.status_code == 409
    assert early_activation.json()["code"] == "PREVIOUS_BOOK_NOT_COMPLETED"
    book = series["books"][0]
    first, second = book["chapters"]
    updated = client.patch(f"/api/chapters/{second['id']}", json={"title":"未来章新标题"})
    assert updated.status_code == 200 and updated.json()["title"] == "未来章新标题"
    added = client.post(f"/api/books/{book['id']}/chapters", json={"title":"临时章","objective":"验证编辑"})
    assert added.status_code == 201
    assert client.delete(f"/api/chapters/{added.json()['id']}").status_code == 204
    client.post(f"/api/chapters/{first['id']}/generate")
    locked_edit = client.patch(f"/api/chapters/{first['id']}", json={"title":"不应允许"})
    assert locked_edit.status_code == 409 and locked_edit.json()["code"] == "CHAPTER_ALREADY_STARTED"
    proposal = client.post(f"/api/books/{book['id']}/chapters/replan")
    assert proposal.status_code == 200 and proposal.json()["requiresConfirmation"]
    confirmed = client.post(f"/api/books/{book['id']}/chapters/replan/{proposal.json()['proposalId']}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["chapters"][1]["title"] == "重规划章节"


def test_book_replan_feedback_produces_a_new_reviewable_proposal(client):
    series = create_series(client)
    book = series["books"][0]
    first = client.post(f"/api/books/{book['id']}/chapters/replan")
    assert first.status_code == 200

    feedback = "第 1 章太浅，请从机制与边界重新组织，并删除重复内容。"
    revised = client.post(
        f"/api/books/{book['id']}/chapters/replan",
        json={
            "feedback": feedback,
            "previousProposalId": first.json()["proposalId"],
        },
    )
    assert revised.status_code == 200
    assert revised.json()["proposalId"] != first.json()["proposalId"]
    request = client.app.state.ai.last_replan_request
    assert request["feedback"] == feedback
    assert request["reviewed_proposal"]["chapters"]

    with client.app.state.sessions() as db:
        revision = db.get(ChapterRevision, revised.json()["proposalId"])
        audit = json.loads(revision.after_json)
        assert audit["feedback"] == feedback
        assert audit["previousProposalId"] == first.json()["proposalId"]


def test_book_replan_feedback_rejects_an_unknown_proposal(client):
    series = create_series(client)
    book = series["books"][0]
    response = client.post(
        f"/api/books/{book['id']}/chapters/replan",
        json={"feedback": "这版范围不对", "previousProposalId": "revision_missing"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "REPLAN_PROPOSAL_NOT_FOUND"


def test_complete_first_book_attachments_and_enter_second(client):
    series = create_series(client)
    first_book = series["books"][0]
    assert client.get(f"/api/books/{first_book['id']}/capstone").json()["status"] == "locked"
    assert client.post(f"/api/books/{first_book['id']}/capstone", json={"content":{"too":"early"}, "attachmentIds":["missing"]}).status_code == 403
    locked_settlement = client.post(
        f"/api/books/{first_book['id']}/settlement"
    )
    assert locked_settlement.status_code == 403
    assert locked_settlement.json()["code"] == "BOOK_SETTLEMENT_LOCKED"
    for chapter_summary in first_book["chapters"]:
        chapter = client.post(f"/api/chapters/{chapter_summary['id']}/generate").json()
        for section in chapter["sections"]:
            generate_and_pass(client, section["id"])
        stored = client.post(f"/api/chapters/{chapter['id']}/practice/attachments", content=b"practice evidence", headers={"x-filename":"practice.txt","content-type":"text/plain"})
        assert stored.status_code == 201
        assert stored.json()["sha256"] == hashlib.sha256(b"practice evidence").hexdigest()
        practice = client.post(f"/api/chapters/{chapter['id']}/practice", json={"content":{"artifact":"evidence"}, "attachmentIds":[stored.json()["id"]]})
        assert practice.status_code == 200 and practice.json()["status"] == "completed" and practice.json()["evidenceMode"] == "file_attachment"
    settlement = client.post(
        f"/api/books/{first_book['id']}/settlement"
    )
    assert settlement.status_code == 200
    assert settlement.json()["status"] == "completed"
    assert settlement.json()["completedChapterCount"] == settlement.json()["chapterCount"]
    assert settlement.json()["completedSectionCount"] == settlement.json()["sectionCount"]
    assert settlement.json()["verificationRate"] >= 80
    assert settlement.json()["ruleVersion"] == "book_settlement_v1"
    replayed_settlement = client.post(
        f"/api/books/{first_book['id']}/settlement"
    )
    assert replayed_settlement.status_code == 200
    assert replayed_settlement.json()["settledAt"] == settlement.json()["settledAt"]
    assert client.get(f"/api/books/{first_book['id']}/capstone").json()["status"] == "completed"
    capstone_file = client.post(f"/api/books/{first_book['id']}/capstone/attachments", content=b"capstone evidence", headers={"x-filename":"capstone.txt","content-type":"text/plain"})
    assert capstone_file.status_code == 201
    oversized = client.post(f"/api/books/{first_book['id']}/capstone/attachments", content=b"x", headers={"content-length": str(10 * 1024 * 1024 + 1)})
    assert oversized.status_code == 413 and oversized.json()["code"] == "ATTACHMENT_TOO_LARGE"
    cross_target = client.post(f"/api/books/{first_book['id']}/capstone", json={"content":{"artifact":"capstone"}, "attachmentIds":[stored.json()["id"]]})
    assert cross_target.status_code == 403 and cross_target.json()["code"] == "ATTACHMENT_INVALID"
    capstone = client.post(f"/api/books/{first_book['id']}/capstone", json={"content":{"artifact":"capstone"}, "attachmentIds":[capstone_file.json()["id"]]})
    assert capstone.status_code == 200 and capstone.json()["status"] == "completed"
    downloaded = client.get(f"/api/attachments/{capstone_file.json()['id']}")
    assert downloaded.status_code == 200 and downloaded.content == b"capstone evidence"
    final = client.get(f"/api/series/{series['id']}").json()
    assert final["books"][0]["status"] == "completed"
    assert final["books"][0]["outlineStatus"] == "confirmed"
    assert final["books"][1]["outlineStatus"] == "draft"
    assert final["books"][1]["status"] == "locked"
    assert 0 < final["progress"] < 100
    draft_chapter = final["books"][1]["chapters"][0]
    blocked = client.post(f"/api/chapters/{draft_chapter['id']}/generate")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "BOOK_OUTLINE_CONFIRMATION_REQUIRED"
    proposal = client.post(
        f"/api/books/{final['books'][1]['id']}/chapters/replan"
    )
    assert proposal.status_code == 200
    activated = client.post(
        f"/api/books/{final['books'][1]['id']}/chapters/replan/"
        f"{proposal.json()['proposalId']}/confirm"
    )
    assert activated.status_code == 200
    assert activated.json()["outlineStatus"] == "confirmed"
    assert activated.json()["status"] == "available"
    final = client.get(f"/api/series/{series['id']}").json()
    dashboard = client.get("/api/bootstrap").json()["milestoneDashboard"]
    live_chapter_ids = {
        chapter["id"]
        for book in final["books"]
        for chapter in book["chapters"]
    }
    assert all(
        criterion["chapterId"] in live_chapter_ids
        for milestone in dashboard["path"]["milestones"]
        for criterion in milestone["criteria"]
    )
    second_chapter = final["books"][1]["chapters"][0]
    assert second_chapter["generated"] is False
    second_chapter = client.post(
        f"/api/chapters/{second_chapter['id']}/generate"
    ).json()
    second_section = client.post(
        f"/api/sections/{second_chapter['sections'][0]['id']}/generate"
    ).json()
    assert second_section["content"] is not None
    assert second_section["generation"]["trace"]["memoryApplied"] is True
    assert second_section["generation"]["trace"]["memoryConceptCount"] > 0


class FailingLessonAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        raise RuntimeError("simulated provider failure")

    def structured_trace(self):
        return [
            {
                "schema": "GeneratedContent",
                "attempts": 2,
                "repairAttempts": 1,
                "outcome": "provider_failed",
                "invalidOutputDigests": ["fedcba9876543210"],
                "lastValidationIssues": [],
            }
        ]


class HarnessTraceAi(FakeAi):
    def structured_trace(self):
        return [
            {
                "schema": "GeneratedContent",
                "attempts": 2,
                "repairAttempts": 1,
                "outcome": "succeeded",
                "invalidOutputDigests": ["0123456789abcdef"],
                "lastValidationIssues": [
                    {
                        "path": "blocks",
                        "type": "too_short",
                        "message": "List should have at least 5 items",
                    }
                ],
            }
        ]


def test_generation_persists_safe_structured_harness_audit():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            HarnessTraceAi(),
            AcceptingSourceVerifier(),
        )
    ) as harness_client:
        series = create_series(harness_client)
        chapter = harness_client.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = harness_client.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()

    trace = section["generation"]["trace"]["aiHarness"]
    assert trace[0]["repairAttempts"] == 1
    assert trace[0]["invalidOutputDigests"] == ["0123456789abcdef"]
    assert "invalid_output" not in json.dumps(trace)


def test_generation_failure_is_observable_and_retry_safe():
    with TestClient(create_app("sqlite+pysqlite:///:memory:", FailingLessonAi(), AcceptingSourceVerifier()), raise_server_exceptions=False) as failing:
        series = create_series(failing)
        assert wait_for_task(
            failing,
            series["initializationTask"]["taskId"],
        )["status"] == "failed"
        chapter = failing.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section_id = chapter["sections"][0]["id"]
        with failing.app.state.sessions() as db:
            progress = db.scalar(
                select(SectionProgress).where(
                    SectionProgress.section_id == section_id
                )
            )
            progress.status = "available"
            db.commit()
        generated = failing.post(f"/api/sections/{section_id}/generate")
        assert generated.status_code == 502, generated.json()
        assert generated.json()["retryable"] is True
        assert generated.json()["operationId"].startswith("generation_")
        state = failing.get(f"/api/sections/{section_id}").json()
        assert state["content"] is None
        assert state["generation"]["status"] == "failed"
        assert state["generation"]["errorCode"] == "RuntimeError"
        assert (
            state["generation"]["trace"]["aiHarness"][0]["outcome"]
            == "provider_failed"
        )


def test_section_regeneration_appends_versions_and_preserves_audit(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    original = client.post(
        f"/api/sections/{chapter['sections'][0]['id']}/generate"
    ).json()

    regenerated = client.post(
        f"/api/sections/{original['id']}/regenerate"
    )

    assert regenerated.status_code == 200
    replacement = regenerated.json()
    assert replacement["content"]["version"] == 2
    assert replacement["content"]["id"] != original["content"]["id"]
    assert replacement["quiz"]["generation"] == 2
    assert replacement["quiz"]["id"] != original["quiz"]["id"]
    assert replacement["generation"]["operation"] == "regeneration"
    assert replacement["generation"]["trace"]["supersedesContentVersionId"] == original["content"]["id"]
    assert replacement["generation"]["trace"]["supersedesQuizSetId"] == original["quiz"]["id"]

    with client.app.state.sessions() as db:
        versions = db.scalars(
            select(ContentVersion)
            .where(ContentVersion.section_id == original["id"])
            .order_by(ContentVersion.version)
        ).all()
        quizzes = db.scalars(
            select(QuizSet)
            .where(QuizSet.section_id == original["id"])
            .order_by(QuizSet.generation)
        ).all()
        runs = db.scalars(
            select(GenerationRun)
            .where(GenerationRun.section_id == original["id"])
            .order_by(GenerationRun.attempt)
        ).all()
    assert [item.id for item in versions] == [original["content"]["id"], replacement["content"]["id"]]
    assert [item.id for item in quizzes] == [original["quiz"]["id"], replacement["quiz"]["id"]]
    assert [item.operation for item in runs] == ["lesson", "regeneration"]

    stale = client.post(
        f"/api/sections/{original['id']}/quiz",
        json={
            "quizSetId": original["quiz"]["id"],
            "answers": [[1] for _ in original["quiz"]["questions"]],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "QUIZ_STALE"

    assessed = client.post(
        f"/api/sections/{original['id']}/quiz",
        json={
            "quizSetId": replacement["quiz"]["id"],
            "answers": [[1] for _ in replacement["quiz"]["questions"]],
        },
    )
    assert assessed.status_code == 200, assessed.json()
    blocked = client.post(f"/api/sections/{original['id']}/regenerate")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "SECTION_ALREADY_ASSESSED"


def test_section_regeneration_upgrades_migrated_bound_contract(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    original = client.post(
        f"/api/sections/{chapter['sections'][0]['id']}/generate"
    ).json()
    opened = client.post(f"/api/sections/{original['id']}/open")
    assert opened.status_code == 200, opened.json()

    with client.app.state.sessions() as db:
        binding = db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.section_id == original["id"]
            )
        )
        contract = db.get(
            LearningContractVersion,
            binding.learning_contract_version_id,
        )
        stored_section = db.get(Section, original["id"])
        migrated_contract = ensure_learning_contract(
            db,
            stored_section,
            mission_version_id=contract.mission_version_id,
            provenance_mode="derived_from_m1",
        )
        content = db.get(ContentVersion, original["content"]["id"])
        quiz = db.get(QuizSet, original["quiz"]["id"])
        content.learning_contract_version_id = migrated_contract.id
        quiz.learning_contract_version_id = migrated_contract.id
        binding.learning_contract_version_id = migrated_contract.id
        content.schema_version = "legacy"
        content.prompt_version = "legacy"
        quiz.schema_version = "legacy"
        decisions = db.scalars(
            select(GovernanceDecisionSnapshot).where(
                GovernanceDecisionSnapshot.quiz_set_id == quiz.id,
                GovernanceDecisionSnapshot.decision_scope == "quiz_publication",
            )
        ).all()
        assert decisions
        for decision in decisions:
            decision.learning_contract_version_id = migrated_contract.id
            decision.allowed = False
            decision.assessment_eligible = False
        migrated_contract_id = migrated_contract.id
        db.commit()

    migrated = client.get(f"/api/sections/{original['id']}").json()
    assert migrated["quiz"]["governance"]["assessmentEligible"] is False

    regenerated = client.post(f"/api/sections/{original['id']}/regenerate")

    assert regenerated.status_code == 200, regenerated.json()
    replacement = regenerated.json()
    assert replacement["content"]["id"] != original["content"]["id"]
    assert replacement["quiz"]["governance"]["assessmentEligible"] is True
    assert (
        replacement["versionBinding"]["learningContractVersionId"]
        == migrated_contract_id
    )
    assert (
        replacement["versionBinding"]["contentVersionId"]
        == replacement["content"]["id"]
    )


def test_content_feedback_streams_the_model_repair_and_rebinds_only_content(client):
    series = create_series(client)
    assert wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )["status"] == "succeeded"
    refreshed = client.get(f"/api/series/{series['id']}").json()
    section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
    original = client.post(f"/api/sections/{section_id}/open").json()
    block = original["content"]["blocks"][0]

    submitted = client.post(
        "/api/feedback",
        headers={"Idempotency-Key": "feedback-auto-regeneration-001"},
        json={
            "scope": "content_block",
            "feedbackType": "unclear",
            "message": "因果关系跳得太快，请把中间机制讲清楚",
            "pagePath": "/",
            "view": "learn",
            "sectionId": section_id,
            "contentVersionId": original["content"]["id"],
            "blockId": block["id"],
        },
    )

    assert submitted.status_code == 201, submitted.json()
    receipt = submitted.json()
    assert receipt["regeneration"] == {
        "status": "stream_ready",
        "reasonCode": None,
        "task": None,
    }
    streamed = client.post(f"/api/feedback/{receipt['id']}/repair/stream")
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert streamed.headers["x-accel-buffering"] == "no"
    events = sse_events(streamed)
    deltas = [event[1]["delta"] for event in events if event[0] == "delta"]
    done = next(event[1] for event in events if event[0] == "done")
    expected_repair = "".join(deltas)
    assert len(deltas) == 4
    assert expected_repair.endswith("表格之外的说明现在是独立段落。")
    replacement = client.get(f"/api/sections/{section_id}").json()
    assert replacement["content"]["version"] == 2
    assert replacement["content"]["id"] != original["content"]["id"]
    assert replacement["content"]["blocks"][0]["content"] == expected_repair
    assert replacement["content"]["blocks"][0]["kind"] == "text"
    assert replacement["quiz"]["id"] == original["quiz"]["id"]
    assert replacement["versionBinding"]["contentVersionId"] == replacement["content"]["id"]
    assert replacement["versionBinding"]["initialQuizSetId"] == original["quiz"]["id"]
    assert done["contentVersionId"] == replacement["content"]["id"]
    repair_request = client.app.state.ai.last_repair_request
    assert repair_request["targetBlock"]["id"] == block["id"]
    assert repair_request["feedback"] == {
        "type": "unclear",
        "message": "因果关系跳得太快，请把中间机制讲清楚",
    }
    with client.app.state.sessions() as db:
        versions = db.scalars(
            select(ContentVersion)
            .where(ContentVersion.section_id == section_id)
            .order_by(ContentVersion.version)
        ).all()
        generation = db.scalar(
            select(GenerationRun)
            .where(
                GenerationRun.section_id == section_id,
                GenerationRun.operation == "feedback_repair",
            )
        )
        binding = db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.section_id == section_id
            )
        )
    assert [item.id for item in versions] == [
        original["content"]["id"],
        replacement["content"]["id"],
    ]
    assert json.loads(generation.trace_json)["feedbackId"] == receipt["id"]
    assert json.loads(binding.lineage_audit_json)["feedbackRepairs"][0][
        "feedbackId"
    ] == receipt["id"]
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(func.count())
            .select_from(LearningTask)
            .where(LearningTask.task_type == "content_feedback_regeneration")
        ) == 0

    replayed = client.post(f"/api/feedback/{receipt['id']}/repair/stream")
    replay_events = sse_events(replayed)
    replay_done = next(event[1] for event in replay_events if event[0] == "done")
    assert replay_done["replayed"] is True
    assert replay_done["contentVersionId"] == replacement["content"]["id"]
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ContentVersion)
            .where(ContentVersion.section_id == section_id)
        ) == 2


def test_content_feedback_repairs_legacy_contract_bound_content(client):
    series = create_series(client)
    assert wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )["status"] == "succeeded"
    refreshed = client.get(f"/api/series/{series['id']}").json()
    section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
    original = client.post(f"/api/sections/{section_id}/open").json()
    with client.app.state.sessions() as db:
        contract = db.get(
            LearningContractVersion,
            original["versionBinding"]["learningContractVersionId"],
        )
        contract.provenance_mode = "derived_from_m1"
        contract.lineage_status = "provisional"
        contract.contract_hash = "f" * 64
        db.commit()

    block = original["content"]["blocks"][0]
    submitted = client.post(
        "/api/feedback",
        headers={"Idempotency-Key": "feedback-legacy-contract-001"},
        json={
            "scope": "content_block",
            "feedbackType": "layout",
            "message": "请修正段落层次和间距",
            "sectionId": section_id,
            "contentVersionId": original["content"]["id"],
            "blockId": block["id"],
        },
    )

    assert submitted.status_code == 201, submitted.json()
    receipt = submitted.json()
    assert receipt["regeneration"]["status"] == "stream_ready"
    events = sse_events(
        client.post(f"/api/feedback/{receipt['id']}/repair/stream")
    )
    assert any(event[0] == "done" for event in events)
    replacement = client.get(f"/api/sections/{section_id}").json()
    assert replacement["content"]["version"] == 2
    assert (
        replacement["versionBinding"]["learningContractVersionId"]
        == original["versionBinding"]["learningContractVersionId"]
    )


def test_accuracy_feedback_after_assessment_preserves_content_and_evidence(client):
    series = create_series(client)
    assert wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )["status"] == "succeeded"
    refreshed = client.get(f"/api/series/{series['id']}").json()
    section_id = refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
    section = client.post(f"/api/sections/{section_id}/open").json()
    assessed = client.post(
        f"/api/sections/{section_id}/quiz",
        json={
            "quizSetId": section["quiz"]["id"],
            "answers": [[1] for _ in section["quiz"]["questions"]],
        },
    )
    assert assessed.status_code == 200
    attempt_id = assessed.json()["attemptId"]
    block = section["content"]["blocks"][0]

    submitted = client.post(
        "/api/feedback",
        headers={"Idempotency-Key": "feedback-after-assessment-001"},
        json={
            "scope": "content_block",
            "feedbackType": "inaccurate",
            "message": "这一段需要纠错",
            "sectionId": section_id,
            "contentVersionId": section["content"]["id"],
            "blockId": block["id"],
        },
    )

    assert submitted.status_code == 201
    receipt = submitted.json()
    assert receipt["regeneration"] == {
        "status": "needs_review",
        "reasonCode": "FEEDBACK_ACCURACY_REVIEW_REQUIRED",
        "task": None,
    }
    repair = client.post(f"/api/feedback/{receipt['id']}/repair/stream")
    assert repair.status_code == 200
    error_event = next(
        data for event, data in sse_events(repair) if event == "error"
    )
    assert error_event["code"] == (
        "FEEDBACK_ACCURACY_REVIEW_REQUIRED"
    )
    replacement = client.get(f"/api/sections/{section_id}").json()
    assert replacement["status"] == "completed"
    assert replacement["content"]["version"] == 1
    assert replacement["content"]["id"] == section["content"]["id"]
    assert replacement["quiz"]["id"] == section["quiz"]["id"]
    assert replacement["latestAttemptReview"] is not None
    with client.app.state.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(UserFeedback)
        ) == 1
        feedback_task_count = db.scalar(
            select(func.count()).select_from(LearningTask).where(
                LearningTask.task_type == "content_feedback_regeneration"
            )
        )
        original_attempt = db.get(QuizAttempt, attempt_id)
        original_quiz = db.get(QuizSet, section["quiz"]["id"])
        binding = db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.section_id == section_id
            )
        )
    assert feedback_task_count == 0
    assert original_attempt.quiz_set_id == section["quiz"]["id"]
    assert original_quiz.content_version_id == section["content"]["id"]
    assert binding.content_version_id == section["content"]["id"]
    assert binding.initial_quiz_set_id == section["quiz"]["id"]


def test_failed_content_feedback_repair_stream_preserves_the_visible_version(
    tmp_path,
):
    storage = LocalAttachmentStorage(tmp_path / "feedback-failure-attachments")
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            FeedbackRegenerationFailingAi(),
            AcceptingSourceVerifier(),
            storage,
        )
    ) as failing_client:
        series = create_series(failing_client)
        assert wait_for_task(
            failing_client,
            series["initializationTask"]["taskId"],
        )["status"] == "succeeded"
        refreshed = failing_client.get(f"/api/series/{series['id']}").json()
        section_id = (
            refreshed["books"][0]["chapters"][0]["sections"][0]["id"]
        )
        original = failing_client.post(
            f"/api/sections/{section_id}/open"
        ).json()
        block = original["content"]["blocks"][0]
        submitted = failing_client.post(
            "/api/feedback",
            headers={"Idempotency-Key": "feedback-regeneration-failure-001"},
            json={
                "scope": "content_block",
                "feedbackType": "poor_example",
                "message": "请更换这个例子",
                "sectionId": section_id,
                "contentVersionId": original["content"]["id"],
                "blockId": block["id"],
            },
        )
        receipt = submitted.json()
        streamed = failing_client.post(
            f"/api/feedback/{receipt['id']}/repair/stream"
        )
        events = sse_events(streamed)
        error = next(event[1] for event in events if event[0] == "error")
        assert error["code"] == "FEEDBACK_REPAIR_TEST_FAILURE"
        assert error["message"] == "反馈修订生成失败"
        assert error["retryable"] is False
        current = failing_client.get(f"/api/sections/{section_id}").json()
        assert current["content"]["id"] == original["content"]["id"]
        assert current["quiz"]["id"] == original["quiz"]["id"]
        with failing_client.app.state.sessions() as db:
            assert db.scalar(
                select(func.count())
                .select_from(ContentVersion)
                .where(ContentVersion.section_id == section_id)
            ) == 1
            assert db.scalar(
                select(func.count()).select_from(UserFeedback)
            ) == 1
            failed_run = db.scalar(
                select(GenerationRun).where(
                    GenerationRun.operation == "feedback_repair"
                )
            )
            assert failed_run.status == "failed"
            assert failed_run.error_code == "FEEDBACK_REPAIR_TEST_FAILURE"


class GenerationArtifactAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        lesson = await super().lesson(request, memory, prior_questions)
        lesson.blocks[0].content = "候选 JSON 为空字符串，无法恢复原有事实内容。"
        return lesson


def test_generation_artifact_is_rejected_before_persistence():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            GenerationArtifactAi(),
            AcceptingSourceVerifier(),
        ),
        raise_server_exceptions=False,
    ) as artifact_client:
        series = create_series(artifact_client)
        assert wait_for_task(
            artifact_client,
            series["initializationTask"]["taskId"],
        )["status"] == "failed"
        chapter = artifact_client.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section_id = chapter["sections"][0]["id"]
        with artifact_client.app.state.sessions() as db:
            progress = db.scalar(
                select(SectionProgress).where(
                    SectionProgress.section_id == section_id
                )
            )
            progress.status = "available"
            db.commit()
        response = artifact_client.post(f"/api/sections/{section_id}/generate")

        assert response.status_code == 502, response.json()
        assert response.json()["code"] == "AI_CONTENT_QUALITY_FAILED"
        state = artifact_client.get(f"/api/sections/{section_id}").json()
        assert state["content"] is None
        assert state["generation"]["errorCode"] == "AI_CONTENT_QUALITY_FAILED"


class DuplicateRetryAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        return await super().lesson(request, memory, None)


def test_duplicate_retry_questions_are_reordered_and_published():
    with TestClient(create_app("sqlite+pysqlite:///:memory:", DuplicateRetryAi(), AcceptingSourceVerifier())) as duplicate:
        series = create_series(duplicate)
        chapter = duplicate.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section = duplicate.post(f"/api/sections/{chapter['sections'][0]['id']}/generate").json()
        original_questions = section["quiz"]["questions"]
        failed = duplicate.post(f"/api/sections/{section['id']}/quiz", json={"quizSetId":section["quiz"]["id"],"answers":[[] for _ in section["quiz"]["questions"]]})
        assert failed.status_code == 200
        remediation_task = next(
            task
            for task in failed.json()["workflowTasks"]
            if task["type"] == "remediation_generation"
        )
        task = wait_for_task(duplicate, remediation_task["taskId"])
        assert task["status"] == "succeeded"
        state = duplicate.get(f"/api/sections/{section['id']}").json()
        assert state["quiz"]["generation"] == 2
        assert all(
            replacement["options"] != original["options"]
            for replacement, original in zip(
                state["quiz"]["questions"],
                original_questions,
                strict=True,
            )
        )
        with duplicate.app.state.sessions() as db:
            exhausted = db.get(LearningTask, remediation_task["taskId"])
            exhausted.status = "failed"
            exhausted.error_code = "QUIZ_NOT_NOVEL"
            exhausted.attempt_count = exhausted.max_attempts
            db.commit()
        reset = duplicate.post(
            f"/api/learning-tasks/{remediation_task['taskId']}/retry"
        )
        assert reset.status_code == 200
        assert reset.json()["status"] == "pending"
        assert reset.json()["maxAttempts"] == 6


def test_source_code_requires_immutable_matching_github_ref():
    with pytest.raises(ValidationError):
        Source(title="bad", url="https://github.com/kubernetes/kubernetes/blob/main/pkg/api.go", kind="source_code", version="main")
    with pytest.raises(ValidationError):
        Source(title="mismatch", url="https://github.com/kubernetes/kubernetes/blob/v1.30.0/pkg/api.go", kind="source_code", version="v1.29.0")
    source = Source(title="pinned", url="https://github.com/kubernetes/kubernetes/blob/v1.30.0/pkg/api.go", kind="source_code", version="v1.30.0")
    assert source.version == "v1.30.0"


def test_generated_content_records_governance_gap_without_promoting_reachability(client):
    series = create_series(client)
    chapter = client.post(
        f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
    ).json()
    section = client.post(
        f"/api/sections/{chapter['sections'][0]['id']}/generate"
    ).json()

    governance = section["quiz"]["governance"]
    assert governance["allowed"] is True
    assert governance["assessmentEligible"] is True
    assert governance["mode"] == "formal"

    with client.app.state.sessions() as db:
        assert db.scalars(
            select(ContentBlockVersion).where(
                ContentBlockVersion.content_version_id == section["content"]["id"]
            )
        ).all()
        assert db.scalars(
            select(KnowledgeGap).where(
                KnowledgeGap.content_version_id == section["content"]["id"]
            )
        ).all()
        bindings = db.scalars(
            select(SourceClaimBinding)
            .join(
                ContentBlockClaimAnchor,
                ContentBlockClaimAnchor.source_claim_version_id
                == SourceClaimBinding.source_claim_version_id,
            )
            .join(
                ContentBlockVersion,
                ContentBlockVersion.id
                == ContentBlockClaimAnchor.content_block_version_id,
            )
            .where(ContentBlockVersion.content_version_id == section["content"]["id"])
        ).all()
        assert bindings
        reachability_bindings = [
            item for item in bindings
            if item.verification_mode == "reachability_only"
        ]
        assert reachability_bindings
        assert {item.verification_status for item in reachability_bindings} == {
            "unverified"
        }
        assert not any(item.verified_at for item in reachability_bindings)
        decisions = db.scalars(
            select(GovernanceDecisionSnapshot).where(
                GovernanceDecisionSnapshot.quiz_set_id == section["quiz"]["id"]
            )
        ).all()
        assert any(not item.allowed for item in decisions)
        assert any(item.allowed and item.assessment_eligible for item in decisions)

        source = db.scalar(
            select(SourceVersion).where(
                SourceVersion.content_version_id == section["content"]["id"]
            )
        )
        claims = db.scalars(
            select(SourceClaimVersion).where(
                SourceClaimVersion.id.in_(
                    [item.source_claim_version_id for item in bindings]
                )
            )
        ).all()
        for claim in claims:
            record_verified_claim_binding(
                db,
                source_claim_version_id=claim.id,
                source_version_id=source.id,
                locator_type="official_section",
                locator={"heading": claim.claim_kind},
                excerpt_text=claim.statement,
                support_type="supports",
                verification_mode="exact_excerpt_and_entailment",
                verification_rule_version="claim_support_v1",
                report={"entails": True, "reviewedBy": "test_verifier"},
                actor_id="test_claim_verifier",
            )
        replay = reevaluate_generated_governance(
            db,
            quiz_id=section["quiz"]["id"],
            actor_id="test_claim_verifier",
        )
        assert replay["allowed"] is True
        assert replay["assessmentEligible"] is True
        assert replay["mode"] == "formal"


def test_missing_generated_lineage_cannot_be_inferred_into_formal_evidence(tmp_path):
    with TestClient(create_app(
        "sqlite+pysqlite:///:memory:",
        MissingLineageAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "missing-lineage-attachments"),
    )) as lineage_client:
        series = create_series(lineage_client)
        assert wait_for_task(
            lineage_client,
            series["initializationTask"]["taskId"],
        )["status"] == "succeeded"
        view = lineage_client.get(f"/api/series/{series['id']}").json()
        section_id = view["books"][0]["chapters"][0]["sections"][0]["id"]
        governance = lineage_client.get(
            f"/api/sections/{section_id}"
        ).json()["quiz"]["governance"]

        assert governance["allowed"] is False
        assert governance["assessmentEligible"] is False
        assert any(
            reason["code"] in {"QUESTION_TARGET_NOT_TAUGHT", "QUESTION_CLAIM_REQUIRED"}
            for reason in governance["reasons"]
        )


def test_unverified_governance_blocks_submission_and_all_evidence():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            FakeAi(),
            ReachabilityOnlyVerifier(),
        )
    ) as compatibility:
        series = create_series(compatibility)
        chapter = compatibility.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = compatibility.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        assert section["quiz"]["governance"]["allowed"] is False
        submitted = compatibility.post(
            f"/api/sections/{section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1] for _ in section["quiz"]["questions"]],
            },
        )
        assert submitted.status_code == 409
        assert submitted.json()["code"] == "QUIZ_GOVERNANCE_REQUIRED"
        with compatibility.app.state.sessions() as db:
            attempts = db.scalars(
                select(QuizAttempt).where(
                    QuizAttempt.quiz_set_id == section["quiz"]["id"]
                )
            ).all()
            assert attempts == []
            assert db.scalars(select(AssessmentObservation)).all() == []
            assert db.scalars(select(EvidenceQualificationEvent)).all() == []
            rebuild_user_projections(db, user_id="user_demo")
        assert compatibility.get(
            "/api/learning-memory?shelf_id=shelf_technology"
        ).json() == []


def test_unverified_remediation_cannot_bypass_quiz_governance():
    with TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            FakeAi(),
            AcceptingSourceVerifier(),
        )
    ) as compatibility:
        series = create_series(compatibility)
        chapter = compatibility.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = compatibility.post(
            f"/api/sections/{chapter['sections'][0]['id']}/generate"
        ).json()
        answers = [[1] for _ in section["quiz"]["questions"]]
        answers[0] = [0]
        answers[1] = [0]
        failed = compatibility.post(
            f"/api/sections/{section['id']}/quiz",
            json={"quizSetId": section["quiz"]["id"], "answers": answers},
        ).json()
        remediation_task = next(
            item
            for item in failed["workflowTasks"]
            if item["type"] == "remediation_generation"
        )
        assert wait_for_task(
            compatibility,
            remediation_task["taskId"],
        )["status"] == "succeeded"

        replacement = compatibility.get(
            f"/api/sections/{section['id']}"
        ).json()["quiz"]
        assert replacement["governance"]["assessmentEligible"] is True
        with compatibility.app.state.sessions() as db:
            knowledge_state_count = db.scalar(
                select(func.count()).select_from(KnowledgeStateProjection)
            )
            previous = db.scalar(
                select(GovernanceDecisionSnapshot)
                .where(
                    GovernanceDecisionSnapshot.quiz_set_id == replacement["id"],
                    GovernanceDecisionSnapshot.decision_scope == "quiz_publication",
                )
                .order_by(GovernanceDecisionSnapshot.created_at.desc())
            )
            db.add(GovernanceDecisionSnapshot(
                id="governance_rejected_remediation",
                decision_scope="quiz_publication",
                content_version_id=previous.content_version_id,
                quiz_set_id=previous.quiz_set_id,
                learning_contract_version_id=previous.learning_contract_version_id,
                requested_mode="formal",
                mode="rejected",
                allowed=False,
                assessment_eligible=False,
                reasons_json='[{"code":"TEST_REJECTION"}]',
                rule_version="test_rejection_v1",
                input_hash="0" * 64,
                actor_kind="test_governance",
                actor_id="test_governance",
                idempotency_key="test-rejected-remediation",
                created_at=now() + timedelta(seconds=1),
            ))
            db.commit()
        resolved = compatibility.post(
            f"/api/sections/{section['id']}/quiz",
            json={
                "quizSetId": replacement["id"],
                "answers": [[1] for _ in replacement["questions"]],
            },
        )
        assert resolved.status_code == 409
        assert resolved.json()["code"] == "QUIZ_GOVERNANCE_REQUIRED"

        with compatibility.app.state.sessions() as db:
            observations = db.scalars(
                select(AssessmentObservation).where(
                    AssessmentObservation.quiz_set_id == replacement["id"]
                )
            ).all()
            assert observations == []
            assert db.scalar(
                select(func.count()).select_from(KnowledgeStateProjection)
            ) == knowledge_state_count


def test_invalid_remediation_candidate_never_becomes_published_quiz(tmp_path):
    class InvalidRemediationAi(FakeAi):
        async def lesson(self, request, memory, prior_questions=None):
            lesson = await super().lesson(request, memory, prior_questions)
            if not prior_questions:
                return lesson
            return lesson.model_copy(update={
                "blocks": [
                    block.model_copy(update={"assessment_objectives": []})
                    for block in lesson.blocks
                ],
            })

    with TestClient(create_app(
        f"sqlite+pysqlite:///{tmp_path / 'invalid-remediation.db'}",
        InvalidRemediationAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "invalid-remediation-attachments"),
    )) as rejected:
        series = create_series(rejected)
        initialization = wait_for_task(
            rejected,
            series["initializationTask"]["taskId"],
        )
        assert initialization["status"] == "succeeded"
        section_id = initialization["result"]["targetSectionId"]
        section = rejected.get(f"/api/sections/{section_id}").json()
        answers = [[1] for _ in section["quiz"]["questions"]]
        answers[0] = [0]
        answers[1] = [0]
        failed = rejected.post(
            f"/api/sections/{section_id}/quiz",
            json={"quizSetId": section["quiz"]["id"], "answers": answers},
        ).json()
        remediation_task = next(
            task
            for task in failed["workflowTasks"]
            if task["type"] == "remediation_generation"
        )
        task = wait_for_task(
            rejected,
            remediation_task["taskId"],
            timeout=5,
        )
        assert task["status"] == "failed"
        assert task["attemptCount"] == task["maxAttempts"]
        assert task["errorCode"] == "REMEDIATION_TARGET_NOT_TAUGHT"

        refreshed = rejected.get(f"/api/sections/{section_id}").json()
        assert refreshed["quiz"]["id"] == section["quiz"]["id"]
        assert refreshed["remediations"] == []
        with rejected.app.state.sessions() as db:
            quizzes = db.scalars(
                select(QuizSet).where(QuizSet.section_id == section_id)
            ).all()
            assert [quiz.id for quiz in quizzes] == [section["quiz"]["id"]]
            assert db.scalars(
                select(Remediation).where(Remediation.section_id == section_id)
            ).all() == []


def test_exhausted_next_section_preload_restores_progress_and_can_retry(tmp_path):
    class ExhaustedPreloadAi(FakeAi):
        def __init__(self):
            self.fail_preload = True
            self.lesson_calls = 0

        async def lesson(self, request, memory, prior_questions=None):
            self.lesson_calls += 1
            if self.fail_preload and self.lesson_calls > 1:
                raise AiError("simulated exhausted preload")
            return await super().lesson(request, memory, prior_questions)

    ai = ExhaustedPreloadAi()
    with TestClient(create_app(
        f"sqlite+pysqlite:///{tmp_path / 'exhausted-preload.db'}",
        ai,
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "exhausted-preload-attachments"),
    )) as recovering:
        series = create_series(recovering)
        initialization = wait_for_task(
            recovering,
            series["initializationTask"]["taskId"],
        )
        assert initialization["status"] == "succeeded"
        current = recovering.get(f"/api/series/{series['id']}").json()
        first_section = current["books"][0]["chapters"][0]["sections"][0]
        section = recovering.get(f"/api/sections/{first_section['id']}").json()

        with recovering.app.state.sessions() as db:
            lookahead = db.scalar(
                select(LearningTask).where(
                    LearningTask.learning_run_id
                    == db.get(
                        LearningTask,
                        series["initializationTask"]["taskId"],
                    ).learning_run_id,
                    LearningTask.task_type == "section_lookahead_preload",
                )
            )
            assert lookahead is not None
            lookahead_id = lookahead.id
        failed_lookahead = wait_for_task(recovering, lookahead_id, timeout=5)
        assert failed_lookahead["status"] == "failed"

        passed = recovering.post(
            f"/api/sections/{first_section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1] for _ in section["quiz"]["questions"]],
            },
        ).json()
        preload = next(
            task
            for task in passed["workflowTasks"]
            if task["type"] == "next_section_preload"
        )
        with recovering.app.state.sessions() as db:
            queued_payload = json.loads(
                db.get(LearningTask, preload["taskId"]).payload_json
            )
            assert queued_payload == {
                "sourceSectionId": first_section["id"],
                "targetSectionId": current["books"][0]["chapters"][0]["sections"][1]["id"],
            }
        failed = wait_for_task(recovering, preload["taskId"], timeout=5)
        assert failed["status"] == "failed"
        assert failed["attemptCount"] == failed["maxAttempts"]
        assert failed["retryable"] is True

        stalled = recovering.get(f"/api/series/{series['id']}").json()
        next_section = stalled["books"][0]["chapters"][0]["sections"][1]
        assert next_section["status"] == "available"
        assert recovering.get(
            f"/api/sections/{next_section['id']}"
        ).status_code == 200

        ai.fail_preload = False
        retried = recovering.post(
            f"/api/learning-tasks/{preload['taskId']}/retry"
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "pending"
        completed = wait_for_task(recovering, preload["taskId"], timeout=5)
        assert completed["status"] == "succeeded"
        assert completed["attemptCount"] == failed["attemptCount"] + 1
        assert completed["maxAttempts"] == failed["attemptCount"] + 3
        ready = recovering.get(f"/api/sections/{next_section['id']}")
        assert ready.status_code == 200
        assert ready.json()["content"] is not None


def test_prepare_section_repairs_orphaned_preparing_state_and_freezes_pair(tmp_path):
    with TestClient(create_app(
        f"sqlite+pysqlite:///{tmp_path / 'orphaned-prepare.db'}",
        FakeAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "orphaned-prepare-attachments"),
    )) as recovering:
        series = create_series(recovering)
        initialization = wait_for_task(
            recovering,
            series["initializationTask"]["taskId"],
        )
        section_id = initialization["result"]["targetSectionId"]
        with recovering.app.state.sessions() as db:
            progress = db.scalar(
                select(SectionProgress).where(
                    SectionProgress.section_id == section_id,
                )
            )
            progress.status = "preparing"
            db.commit()

        prepared = recovering.post(f"/api/sections/{section_id}/prepare")
        assert prepared.status_code == 200
        payload = prepared.json()
        assert payload["status"] == "available"
        assert payload["content"] is not None
        assert payload["quiz"] is not None
        assert payload["versionBinding"] is not None


def test_prepare_section_does_not_steal_an_active_preload(tmp_path):
    with TestClient(create_app(
        f"sqlite+pysqlite:///{tmp_path / 'owned-prepare.db'}",
        FakeAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "owned-prepare-attachments"),
    )) as recovering:
        series = create_series(recovering)
        initialization = wait_for_task(
            recovering,
            series["initializationTask"]["taskId"],
        )
        source_section_id = initialization["result"]["targetSectionId"]
        route = recovering.get(f"/api/series/{series['id']}").json()
        section_id = route["books"][0]["chapters"][0]["sections"][1]["id"]
        with recovering.app.state.sessions() as db:
            initial_task = db.get(
                LearningTask,
                series["initializationTask"]["taskId"],
            )
            progress = db.scalar(
                select(SectionProgress).where(
                    SectionProgress.section_id == section_id,
                )
            )
            progress.status = "preparing"
            db.add(LearningTask(
                id=f"task_{uuid4().hex}",
                learning_run_id=initial_task.learning_run_id,
                user_id=initial_task.user_id,
                section_id=source_section_id,
                task_type="next_section_preload",
                idempotency_key=f"owned:{section_id}",
                trigger_id="test-active-owner",
                # Legacy active tasks may not have persisted targetSectionId.
                payload_json=json.dumps({"sourceSectionId": source_section_id}),
                status="running",
                attempt_count=1,
                max_attempts=3,
                lease_owner="test-worker",
                lease_token=f"lease_{uuid4().hex}",
                lease_expires_at=now() + timedelta(minutes=1),
                heartbeat_at=now(),
            ))
            db.commit()

        blocked = recovering.post(f"/api/sections/{section_id}/prepare")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "SECTION_PREPARING"


def test_lookahead_prepares_one_locked_section_without_unlocking_it(tmp_path):
    ai = FakeAi()
    with TestClient(create_app(
        f"sqlite+pysqlite:///{tmp_path / 'lookahead-buffer.db'}",
        ai,
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "lookahead-buffer-attachments"),
    )) as buffered:
        series = create_series(buffered)
        initialization = wait_for_task(
            buffered,
            series["initializationTask"]["taskId"],
        )
        assert initialization["status"] == "succeeded"
        with buffered.app.state.sessions() as db:
            lookahead = db.scalar(
                select(LearningTask).where(
                    LearningTask.learning_run_id
                    == db.get(
                        LearningTask,
                        series["initializationTask"]["taskId"],
                    ).learning_run_id,
                    LearningTask.task_type == "section_lookahead_preload",
                )
            )
            assert lookahead is not None
            lookahead_id = lookahead.id
        prepared = wait_for_task(buffered, lookahead_id, timeout=5)
        assert prepared["status"] == "succeeded"
        target_id = prepared["result"]["targetSectionId"]

        route = buffered.get(f"/api/series/{series['id']}").json()
        sections = route["books"][0]["chapters"][0]["sections"]
        assert sections[0]["status"] == "available"
        assert sections[1]["id"] == target_id
        assert sections[1]["status"] == "locked"
        assert buffered.get(f"/api/sections/{target_id}").status_code == 403
        with buffered.app.state.sessions() as db:
            content = db.scalar(
                select(ContentVersion).where(
                    ContentVersion.section_id == target_id,
                    ContentVersion.publication_status == "published",
                )
            )
            quiz = db.scalar(
                select(QuizSet).where(
                    QuizSet.section_id == target_id,
                    QuizSet.publication_status == "published",
                )
            )
            assert content is not None
            assert quiz is not None
            assert quiz.content_version_id == content.id


def test_lookahead_crosses_chapter_but_stops_at_book_boundary(tmp_path):
    ai = FakeAi()
    with TestClient(create_app(
        f"sqlite+pysqlite:///{tmp_path / 'lookahead-chapter-boundary.db'}",
        ai,
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "lookahead-chapter-boundary-attachments"),
    )) as buffered:
        series = create_series(buffered)
        initialization = wait_for_task(
            buffered,
            series["initializationTask"]["taskId"],
        )
        assert initialization["status"] == "succeeded"

        with buffered.app.state.sessions() as db:
            initial_task = db.get(
                LearningTask,
                series["initializationTask"]["taskId"],
            )
            initial_lookahead = db.scalar(
                select(LearningTask).where(
                    LearningTask.learning_run_id == initial_task.learning_run_id,
                    LearningTask.task_type == "section_lookahead_preload",
                )
            )
            assert initial_lookahead is not None
            initial_lookahead_id = initial_lookahead.id
        assert wait_for_task(buffered, initial_lookahead_id)["status"] == "succeeded"

        route = buffered.get(f"/api/series/{series['id']}").json()
        first_book = route["books"][0]
        first_chapter = first_book["chapters"][0]
        next_chapter = first_book["chapters"][1]
        assert len(first_chapter["sections"]) == 3
        assert next_chapter["sections"] == []
        source_section_id = first_chapter["sections"][-1]["id"]

        cross_chapter_task_id = f"task_{uuid4().hex}"
        with buffered.app.state.sessions() as db:
            initial_task = db.get(
                LearningTask,
                series["initializationTask"]["taskId"],
            )
            db.add(LearningTask(
                id=cross_chapter_task_id,
                learning_run_id=initial_task.learning_run_id,
                user_id=initial_task.user_id,
                section_id=source_section_id,
                task_type="section_lookahead_preload",
                idempotency_key=f"lookahead-after:{source_section_id}",
                trigger_id="test-cross-chapter-lookahead",
                payload_json=json.dumps({"sourceSectionId": source_section_id}),
                status="pending",
            ))
            db.commit()
        buffered.app.state.learning_task_wakeup.set()

        prepared = wait_for_task(buffered, cross_chapter_task_id, timeout=5)
        assert prepared["status"] == "succeeded"
        assert prepared["result"]["endOfBook"] is False
        target_id = prepared["result"]["targetSectionId"]

        route = buffered.get(f"/api/series/{series['id']}").json()
        first_book = route["books"][0]
        generated_next_chapter = first_book["chapters"][1]
        assert len(generated_next_chapter["sections"]) == 3
        assert generated_next_chapter["sections"][0]["id"] == target_id
        assert all(
            section["status"] == "locked"
            for section in generated_next_chapter["sections"]
        )
        assert buffered.get(f"/api/sections/{target_id}").status_code == 403
        with buffered.app.state.sessions() as db:
            assert db.scalar(select(ContentVersion).where(
                ContentVersion.section_id == target_id,
                ContentVersion.publication_status == "published",
            )) is not None
            assert db.scalar(select(QuizSet).where(
                QuizSet.section_id == target_id,
                QuizSet.publication_status == "published",
            )) is not None

        last_section_id = generated_next_chapter["sections"][-1]["id"]
        book_boundary_task_id = f"task_{uuid4().hex}"
        with buffered.app.state.sessions() as db:
            initial_task = db.get(
                LearningTask,
                series["initializationTask"]["taskId"],
            )
            db.add(LearningTask(
                id=book_boundary_task_id,
                learning_run_id=initial_task.learning_run_id,
                user_id=initial_task.user_id,
                section_id=last_section_id,
                task_type="section_lookahead_preload",
                idempotency_key=f"lookahead-after:{last_section_id}",
                trigger_id="test-book-boundary-lookahead",
                payload_json=json.dumps({"sourceSectionId": last_section_id}),
                status="pending",
            ))
            db.commit()
        buffered.app.state.learning_task_wakeup.set()

        stopped = wait_for_task(buffered, book_boundary_task_id, timeout=5)
        assert stopped["status"] == "succeeded"
        assert stopped["result"]["targetSectionId"] is None
        assert stopped["result"]["endOfBook"] is True
        route = buffered.get(f"/api/series/{series['id']}").json()
        assert route["books"][1]["chapters"][0]["sections"] == []


def test_runner_persists_failure_report_and_evidence_snapshot(tmp_path, monkeypatch):
    def fail_after_first_step(self):
        self.request("GET", "/api/health")
        raise RuntimeError("forced runner interruption")

    monkeypatch.setattr("app.evaluation.runner.LearnerRunner.run", fail_after_first_step)
    database = tmp_path / "evaluation.db"
    output = tmp_path / "reports"
    json_path, markdown_path, report = run(output, database_path_override=database, deterministic={"backendTests": True, "frontendBuild": True})
    assert json_path.is_file() and markdown_path.is_file()
    assert report["review"]["verdict"] == "FAIL"
    assert report["runnerError"]["message"] == "forced runner interruption"
    assert report["deterministic"]["runnerPersistence"] is True
    assert report["evidenceSnapshot"]["database"]["sha256"]
    assert str(tmp_path) not in json_path.read_text(encoding="utf-8")
    assert report["databaseSource"].startswith(("<temporary>/", "<external>/"))
    assert report["databaseSource"].endswith("/evaluation.db")
    assert report["evidenceSnapshot"]["database"]["path"].startswith(
        ("<temporary>/", "<external>/")
    )
    with sqlite3.connect(database) as connection:
        saved = connection.execute("select status, result_json from evaluation_runs where id = ?", (report["runId"],)).fetchone()
    assert saved and saved[0] == "fail" and "forced runner interruption" in saved[1]
