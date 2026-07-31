import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from app.ai.contracts import AskMeTurn, ClassifiedAnswer, ContentBlock, GeneratedChapter, GeneratedLesson, GeneratedNote, GeneratedPlan, GeneratedSectionOutline, PlanBook, PlanChapter, ReplannedBook, ReplannedChapter, Source, ChoiceQuestion
from app.main import create_app
from app.evaluation.runner import run
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier
from app.infrastructure.tables import (
    Book,
    ChapterRevision,
    LearningEvidence,
    LearningTask,
    LearningPlan,
    LearningRun,
    QuizAttempt,
    Section,
    SectionProgress,
    Series,
)


class FakeAi:
    configured, model = True, "fake-structured"
    async def close(self): pass
    async def plan(self, request, memory):
        return GeneratedPlan(series_title="K8s 台阶", rationale="从对象到排障", assumptions=[], confidence="high", books=[PlanBook(title="K8s（一）基础", topic="K8s", description="核心对象", estimated_minutes=300, chapters=[PlanChapter(title="Pod 与调度", objective="理解对象和调度"), PlanChapter(title="部署", objective="完成基础部署")]), PlanBook(title="K8s（二）网络", topic="K8s 网络", description="网络与服务", estimated_minutes=300, chapters=[PlanChapter(title="Service", objective="理解服务发现"), PlanChapter(title="排障", objective="定位网络问题")])])
    async def chapter(self, request, memory):
        return GeneratedChapter(sections=[GeneratedSectionOutline(title=f"第{i}节", question=f"问题{i}", objectives=[f"目标{i}"]) for i in range(1,4)])
    async def lesson(self, request, memory, prior_questions=None):
        generation = 2 if prior_questions else 1
        roles = ["conclusion","mechanism","example","boundary","practice"]
        return GeneratedLesson(confidence="high", sources=[Source(title="Kubernetes Docs", url="https://kubernetes.io/docs/", kind="official", version="v1.30")], blocks=[ContentBlock(kind="text", role=role, heading=role, content=f"{role} 内容", source_indexes=[0]) for role in roles], questions=[ChoiceQuestion(prompt=f"第{generation}套题{i}", options=[f"A{generation}",f"B{generation}",f"C{generation}"], correct=[1], core=i==0, objective=f"目标{i}", explanation=f"因为 B{generation}") for i in range(5)])
    async def answer(self, request):
        requested = request.get("requestedThreadId")
        return ClassifiedAnswer(relation="follow_up" if requested else "new_question", thread_id=request.get("newThreadId") or requested, answer="基于当前段落回答", thread_summary="已澄清机制")
    async def note(self, request):
        return GeneratedNote(solved_question="解决问题", core_mechanism=["机制"], personal_gaps=[], boundaries=["边界"], practice_checks=["检查"], sources=["Kubernetes Docs"], unresolved=[])
    async def ask_me(self, request):
        dimension = request["dimension"]
        return AskMeTurn(dimension=dimension, prompt=f"请说明 {dimension}", evaluation="not_evaluated" if not request.get("previousAnswer") else "strong", rationale="回答覆盖关键点")
    async def replan_book(self, request, memory):
        return ReplannedBook(rationale="根据学习记忆减少重复", chapters=[ReplannedChapter(title="重规划章节", objective="验证迁移")])


@pytest.fixture
def client(tmp_path):
    storage = LocalAttachmentStorage(tmp_path / "attachments")
    with TestClient(create_app("sqlite+pysqlite:///:memory:", FakeAi(), AcceptingSourceVerifier(), storage)) as value:
        yield value


def test_complete_real_shape_vertical_slice(client):
    plan = client.post("/api/plans", json={"shelfId":"shelf_technology","topic":"Kubernetes","role":"技术人员","experience":"会 Docker","purpose":"参与部署排障","depth":"deep","details":"理解机制"})
    assert plan.status_code == 201
    series = plan.json(); assert len(series["books"]) == 2 and series["books"][1]["status"] == "locked"
    chapter_id = series["books"][0]["chapters"][0]["id"]
    chapter = client.post(f"/api/chapters/{chapter_id}/generate").json()
    section_id = chapter["sections"][0]["id"]
    section = client.post(f"/api/sections/{section_id}/generate").json()
    assert len(section["content"]["blocks"]) == 5
    assert all(block["id"].startswith(f"block_{section['content']['id']}_") for block in section["content"]["blocks"])
    assert all(item["reachable"] for item in section["content"]["sourceVerification"])
    assert all(question["selectionMode"] == "single" for question in section["quiz"]["questions"])
    assert all("correct" not in question for question in section["quiz"]["questions"])
    quiz_id = section["quiz"]["id"]
    failed = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":quiz_id,"answers":[[0],[1],[1],[1],[1]]}).json()
    assert failed["passed"] is False
    remediation_task = next(
        task
        for task in failed["workflowTasks"]
        if task["type"] == "remediation_generation"
    )
    assert wait_for_task(client, remediation_task["taskId"])["status"] == "succeeded"
    remediated = client.get(f"/api/sections/{section_id}").json()
    assert remediated["quiz"]["generation"] == 2
    assert remediated["remediations"][-1]["blocks"]
    stale = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":quiz_id,"answers":[[1],[1],[1],[1],[1]]})
    assert stale.status_code == 409 and stale.json()["code"] == "QUIZ_STALE"
    passed = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":remediated["quiz"]["id"],"answers":[[1],[1],[1],[1],[1]]}).json()
    assert passed["passed"] is True
    for task in passed["workflowTasks"]:
        assert wait_for_task(client, task["taskId"])["status"] == "succeeded"
    completed = client.get(f"/api/sections/{section_id}").json()
    assert completed["note"]
    refreshed_series = client.get(f"/api/series/{series['id']}").json()
    refreshed_sections = refreshed_series["books"][0]["chapters"][0]["sections"]
    assert refreshed_sections[0]["status"] == "completed"
    assert refreshed_sections[1]["status"] == "available"
    next_section = client.get(
        f"/api/sections/{refreshed_sections[1]['id']}"
    ).json()
    assert next_section["content"] is not None
    assert client.get("/api/learning-memory?shelf_id=shelf_technology").json()


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
            lesson.questions[0] = ChoiceQuestion(
                prompt=first.prompt,
                options=first.options,
                correct=[0, 1],
                core=first.core,
                objective=first.objective,
                explanation=first.explanation,
            )
            return lesson

    storage = LocalAttachmentStorage(tmp_path / "mixed-choice-attachments")
    with TestClient(create_app("sqlite+pysqlite:///:memory:", MixedChoiceAi(), AcceptingSourceVerifier(), storage)) as mixed:
        series = create_series(mixed)
        chapter = mixed.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section = mixed.post(f"/api/sections/{chapter['sections'][0]['id']}/generate").json()
        questions = section["quiz"]["questions"]
        assert [question["selectionMode"] for question in questions] == ["multiple", "single", "single", "single", "single"]
        assert all("correct" not in question for question in questions)
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
        assert len(attempts) == 1
        assert len(evidence) == len(section["quiz"]["questions"])

    conflict = client.post(
        f"/api/sections/{section['id']}/quiz",
        json={**body, "answers": [[0] for _ in section["quiz"]["questions"]]},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


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


def test_quiz_response_does_not_wait_for_post_quiz_ai(tmp_path):
    class SlowPostQuizAi(FakeAi):
        async def note(self, request):
            await asyncio.sleep(0.4)
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
        started = time.monotonic()
        response = non_blocking.post(
            f"/api/sections/{section['id']}/quiz",
            json={
                "quizSetId": section["quiz"]["id"],
                "answers": [[1] for _ in section["quiz"]["questions"]],
            },
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert response.json()["passed"] is True
        assert elapsed < 0.25
        assert {
            task["type"] for task in response.json()["workflowTasks"]
        } == {"note_generation", "next_section_preload"}
        for task in response.json()["workflowTasks"]:
            assert wait_for_task(
                non_blocking,
                task["taskId"],
            )["status"] == "succeeded"


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
    assert client.post(f"/api/chapters/{locked}/generate").status_code == 403


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


def test_deleting_available_book_unlocks_next_and_last_book_hides_series(client):
    series = create_series(client)
    first_book, second_book = series["books"]

    assert client.delete(f"/api/books/{first_book['id']}").status_code == 204
    remaining = client.get(f"/api/series/{series['id']}").json()["books"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == second_book["id"]
    assert remaining[0]["status"] == "available"
    assert remaining[0]["chapters"][0]["status"] == "available"

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
    assert sum(item.startswith("SELECT") for item in statements) <= 8
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


def test_future_chapter_edits_and_started_boundary(client):
    series = create_series(client)
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


def test_complete_first_book_attachments_and_enter_second(client):
    series = create_series(client)
    first_book = series["books"][0]
    assert client.get(f"/api/books/{first_book['id']}/capstone").json()["status"] == "locked"
    assert client.post(f"/api/books/{first_book['id']}/capstone", json={"content":{"too":"early"}, "attachmentIds":["missing"]}).status_code == 403
    for chapter_summary in first_book["chapters"]:
        chapter = client.post(f"/api/chapters/{chapter_summary['id']}/generate").json()
        for section in chapter["sections"]:
            generate_and_pass(client, section["id"])
        stored = client.post(f"/api/chapters/{chapter['id']}/practice/attachments", content=b"practice evidence", headers={"x-filename":"practice.txt","content-type":"text/plain"})
        assert stored.status_code == 201
        assert stored.json()["sha256"] == hashlib.sha256(b"practice evidence").hexdigest()
        practice = client.post(f"/api/chapters/{chapter['id']}/practice", json={"content":{"artifact":"evidence"}, "attachmentIds":[stored.json()["id"]]})
        assert practice.status_code == 200 and practice.json()["status"] == "completed" and practice.json()["evidenceMode"] == "file_attachment"
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
    assert final["books"][1]["status"] == "available"
    assert 0 < final["progress"] < 100
    second_chapter = final["books"][1]["chapters"][0]
    assert second_chapter["generated"] is True
    second_section = client.get(
        f"/api/sections/{second_chapter['sections'][0]['id']}"
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
        chapter = failing.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section_id = chapter["sections"][0]["id"]
        generated = failing.post(f"/api/sections/{section_id}/generate")
        assert generated.status_code == 502
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


class DuplicateRetryAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        return await super().lesson(request, memory, None)


def test_duplicate_retry_questions_are_rejected_and_observable():
    with TestClient(create_app("sqlite+pysqlite:///:memory:", DuplicateRetryAi(), AcceptingSourceVerifier())) as duplicate:
        series = create_series(duplicate)
        chapter = duplicate.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section = duplicate.post(f"/api/sections/{chapter['sections'][0]['id']}/generate").json()
        failed = duplicate.post(f"/api/sections/{section['id']}/quiz", json={"quizSetId":section["quiz"]["id"],"answers":[[] for _ in section["quiz"]["questions"]]})
        assert failed.status_code == 200
        remediation_task = next(
            task
            for task in failed.json()["workflowTasks"]
            if task["type"] == "remediation_generation"
        )
        task = wait_for_task(duplicate, remediation_task["taskId"])
        assert task["status"] == "failed"
        assert task["errorCode"] == "QUIZ_NOT_NOVEL"
        state = duplicate.get(f"/api/sections/{section['id']}").json()
        assert state["generation"]["status"] == "failed" and state["generation"]["errorCode"] == "QUIZ_NOT_NOVEL"


def test_source_code_requires_immutable_matching_github_ref():
    with pytest.raises(ValidationError):
        Source(title="bad", url="https://github.com/kubernetes/kubernetes/blob/main/pkg/api.go", kind="source_code", version="main")
    with pytest.raises(ValidationError):
        Source(title="mismatch", url="https://github.com/kubernetes/kubernetes/blob/v1.30.0/pkg/api.go", kind="source_code", version="v1.29.0")
    source = Source(title="pinned", url="https://github.com/kubernetes/kubernetes/blob/v1.30.0/pkg/api.go", kind="source_code", version="v1.30.0")
    assert source.version == "v1.30.0"


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
