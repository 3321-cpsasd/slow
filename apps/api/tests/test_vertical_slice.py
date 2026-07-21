import hashlib
import sqlite3

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from app.ai.contracts import AskMeTurn, ClassifiedAnswer, ContentBlock, GeneratedChapter, GeneratedLesson, GeneratedNote, GeneratedPlan, GeneratedSectionOutline, PlanBook, PlanChapter, ReplannedBook, ReplannedChapter, Source, ChoiceQuestion
from app.main import create_app
from app.evaluation.runner import run
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


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
    quiz_id = section["quiz"]["id"]
    failed = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":quiz_id,"answers":[[0],[1],[1],[1],[1]]}).json()
    assert failed["passed"] is False and failed["nextQuiz"]["generation"] == 2 and failed["remediation"]["blocks"]
    stale = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":quiz_id,"answers":[[1],[1],[1],[1],[1]]})
    assert stale.status_code == 409 and stale.json()["code"] == "QUIZ_STALE"
    passed = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":failed["nextQuiz"]["id"],"answers":[[1],[1],[1],[1],[1]]}).json()
    assert passed["passed"] is True
    completed = client.get(f"/api/sections/{section_id}").json()
    assert completed["note"]
    assert client.get("/api/learning-memory?shelf_id=shelf_technology").json()


def test_locked_boundary(client):
    series = client.post("/api/plans", json={"shelfId":"shelf_technology","topic":"Kubernetes","role":"技术人员","experience":"会 Docker","depth":"deep"}).json()
    locked = series["books"][1]["chapters"][0]["id"]
    assert client.post(f"/api/chapters/{locked}/generate").status_code == 403


def create_series(client):
    return client.post("/api/plans", json={"shelfId":"shelf_technology","topic":"Kubernetes","role":"技术人员","experience":"会 Docker","depth":"deep"}).json()


def generate_and_pass(client, section_id):
    section = client.post(f"/api/sections/{section_id}/generate").json()
    answers = [[1] for _ in section["quiz"]["questions"]]
    result = client.post(f"/api/sections/{section_id}/quiz", json={"quizSetId":section["quiz"]["id"],"answers":answers})
    assert result.status_code == 200 and result.json()["passed"]
    return section


def test_qa_correction_and_three_round_ask_me(client):
    series = create_series(client)
    chapter = client.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
    section = generate_and_pass(client, chapter["sections"][0]["id"])
    block_id = section["content"]["blocks"][0]["id"]
    invalid_block = client.post(f"/api/sections/{section['id']}/ask", json={"blockId":"0","question":"旧数组下标锚点"})
    assert invalid_block.status_code == 409 and invalid_block.json()["code"] == "BLOCK_INVALID"
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
    second_chapter = client.post(f"/api/chapters/{final['books'][1]['chapters'][0]['id']}/generate").json()
    second_section = client.post(f"/api/sections/{second_chapter['sections'][0]['id']}/generate").json()
    assert second_section["generation"]["trace"]["memoryApplied"] is True
    assert second_section["generation"]["trace"]["memoryConceptCount"] > 0


class FailingLessonAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        raise RuntimeError("simulated provider failure")


def test_generation_failure_is_observable_and_retry_safe():
    with TestClient(create_app("sqlite+pysqlite:///:memory:", FailingLessonAi(), AcceptingSourceVerifier()), raise_server_exceptions=False) as failing:
        series = create_series(failing)
        chapter = failing.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section_id = chapter["sections"][0]["id"]
        generated = failing.post(f"/api/sections/{section_id}/generate")
        assert generated.status_code == 502
        state = failing.get(f"/api/sections/{section_id}").json()
        assert state["content"] is None
        assert state["generation"]["status"] == "failed"
        assert state["generation"]["errorCode"] == "RuntimeError"


class DuplicateRetryAi(FakeAi):
    async def lesson(self, request, memory, prior_questions=None):
        return await super().lesson(request, memory, None)


def test_duplicate_retry_questions_are_rejected_and_observable():
    with TestClient(create_app("sqlite+pysqlite:///:memory:", DuplicateRetryAi(), AcceptingSourceVerifier())) as duplicate:
        series = create_series(duplicate)
        chapter = duplicate.post(f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate").json()
        section = duplicate.post(f"/api/sections/{chapter['sections'][0]['id']}/generate").json()
        failed = duplicate.post(f"/api/sections/{section['id']}/quiz", json={"quizSetId":section["quiz"]["id"],"answers":[[] for _ in section["quiz"]["questions"]]})
        assert failed.status_code == 502 and failed.json()["code"] == "QUIZ_NOT_NOVEL"
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
    with sqlite3.connect(database) as connection:
        saved = connection.execute("select status, result_json from evaluation_runs where id = ?", (report["runId"],)).fetchone()
    assert saved and saved[0] == "fail" and "forced runner interruption" in saved[1]
