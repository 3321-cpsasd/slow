import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai.local_adapter import LocalDemoAdapter
from app.application.service import DEMO_USER_ID
from app.infrastructure.tables import (
    Book,
    Chapter,
    ContentVersion,
    LearningPlan,
    LearningTask,
    Section,
    Series,
    Shelf,
    UserFeedback,
)
from app.main import create_app
from app.modules.feedback.service import FEEDBACK_PER_MINUTE_LIMIT
from app.services.source_verifier import AcceptingSourceVerifier


def feedback_client():
    return TestClient(
        create_app(
            "sqlite+pysqlite:///:memory:",
            ai=LocalDemoAdapter(),
            source_verifier=AcceptingSourceVerifier(),
            runtime_settings_path=False,
        )
    )


def feedback_headers(key: str = "feedback-request-0001") -> dict[str, str]:
    return {"Idempotency-Key": key}


def visible_content(client: TestClient):
    with client.app.state.sessions() as db:
        shelf = Shelf(
            id="feedback_shelf",
            user_id=DEMO_USER_ID,
            name="反馈测试书架",
            domain="测试",
        )
        plan = LearningPlan(
            id="feedback_plan",
            shelf_id=shelf.id,
            topic="反馈测试",
            role="测试者",
            experience="测试经验",
            depth="overview",
            confidence="high",
        )
        series = Series(
            id="feedback_series",
            plan_id=plan.id,
            shelf_id=shelf.id,
            title="反馈测试系列",
            rationale="验证反馈锚点",
        )
        book = Book(
            id="feedback_book",
            series_id=series.id,
            shelf_id=shelf.id,
            position=1,
            title="反馈测试教材",
            topic="反馈",
            description="测试",
            estimated_minutes=20,
        )
        chapter = Chapter(
            id="feedback_chapter",
            book_id=book.id,
            position=1,
            title="反馈测试章节",
            objective="验证反馈",
        )
        section = Section(
            id="feedback_section",
            chapter_id=chapter.id,
            position=1,
            title="反馈测试小节",
            question="反馈如何绑定？",
            objectives_json="[]",
        )
        block = {
            "id": "feedback_block_1",
            "version": 1,
            "kind": "text",
            "role": "mechanism",
            "heading": "为什么会形成这个结果",
            "content": "这是一段可被精确绑定的测试正文。",
            "source_indexes": [],
        }
        content = ContentVersion(
            id="feedback_content_1",
            section_id=section.id,
            version=99,
            blocks_json=json.dumps([block], ensure_ascii=False),
            sources_json="[]",
            confidence="high",
        )
        for item in (shelf, plan, series, book, chapter, section, content):
            db.add(item)
            db.flush()
        db.commit()
        return section.id, content.id, block


def test_global_feedback_is_an_immutable_user_scoped_fact():
    with feedback_client() as client:
        response = client.post(
            "/api/feedback",
            headers=feedback_headers(),
            json={
                "scope": "global",
                "feedbackType": "feature",
                "message": "希望可以给书添加收藏标记",
                "pagePath": "/profile?section=account&invite_token=secret",
                "view": "home",
            },
        )

        assert response.status_code == 201
        assert response.json()["status"] == "received"
        with client.app.state.sessions() as db:
            feedback = db.scalar(select(UserFeedback))
            assert feedback.user_id == DEMO_USER_ID
            assert feedback.scope == "global"
            assert feedback.content_version_id is None
            assert feedback.page_path == "/profile"
            assert json.loads(feedback.context_json) == {
                "pagePath": "/profile",
                "regeneration": {
                    "reasonCode": None,
                    "status": "not_applicable",
                    "taskId": None,
                },
                "view": "home",
            }


def test_global_feedback_accepts_knowledge_view():
    with feedback_client() as client:
        response = client.post(
            "/api/feedback",
            headers=feedback_headers("knowledge-feedback-key"),
            json={
                "scope": "global",
                "feedbackType": "experience",
                "message": "知识版图反馈",
                "pagePath": "/knowledge",
                "view": "knowledge",
            },
        )

        assert response.status_code == 201


def test_global_feedback_accepts_review_view():
    with feedback_client() as client:
        response = client.post(
            "/api/feedback",
            headers=feedback_headers("review-feedback-key"),
            json={
                "scope": "global",
                "feedbackType": "experience",
                "message": "复习中心反馈",
                "pagePath": "/review",
                "view": "review",
            },
        )

        assert response.status_code == 201


def test_content_feedback_binds_the_exact_visible_content_block():
    with feedback_client() as client:
        section_id, content_version_id, block = visible_content(client)

        response = client.post(
            "/api/feedback",
            headers=feedback_headers(),
            json={
                "scope": "content_block",
                "feedbackType": "layout",
                "message": "这里的因果关系跳得太快",
                "pagePath": "/",
                "view": "learn",
                "sectionId": section_id,
                "contentVersionId": content_version_id,
                "blockId": block["id"],
            },
        )

        assert response.status_code == 201
        assert response.json()["regeneration"] == {
            "status": "stream_ready",
            "reasonCode": None,
            "task": None,
        }
        with client.app.state.sessions() as db:
            feedback = db.scalar(select(UserFeedback))
            assert feedback.section_id == section_id
            assert feedback.content_version_id == content_version_id
            assert feedback.block_id == block["id"]
            assert feedback.feedback_type == "layout"
            assert len(feedback.block_snapshot_hash) == 64
            assert db.scalar(
                select(func.count()).select_from(ContentVersion)
            ) == 1
            assert db.scalar(
                select(func.count()).select_from(LearningTask).where(
                    LearningTask.task_type == "content_feedback_regeneration"
                )
            ) == 0


def test_content_feedback_records_but_blocks_repair_for_a_stale_version():
    with feedback_client() as client:
        section_id, content_version_id, block = visible_content(client)
        with client.app.state.sessions() as db:
            db.add(ContentVersion(
                id="feedback_content_2",
                section_id=section_id,
                version=100,
                blocks_json=json.dumps([block], ensure_ascii=False),
                sources_json="[]",
                confidence="high",
                publication_status="published",
            ))
            db.commit()

        response = client.post(
            "/api/feedback",
            headers=feedback_headers("feedback-stale-version"),
            json={
                "scope": "content_block",
                "feedbackType": "unclear",
                "message": "这是旧版本上的反馈",
                "sectionId": section_id,
                "contentVersionId": content_version_id,
                "blockId": block["id"],
            },
        )

        assert response.status_code == 201
        assert response.json()["regeneration"] == {
            "status": "blocked",
            "reasonCode": "FEEDBACK_CONTENT_VERSION_STALE",
            "task": None,
        }
        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(UserFeedback)) == 1


def test_content_feedback_rejects_a_block_outside_the_bound_version():
    with feedback_client() as client:
        section_id, content_version_id, _block = visible_content(client)

        response = client.post(
            "/api/feedback",
            headers=feedback_headers(),
            json={
                "scope": "content_block",
                "feedbackType": "inaccurate",
                "message": "这段内容似乎不对",
                "sectionId": section_id,
                "contentVersionId": content_version_id,
                "blockId": "block_from_another_version",
            },
        )

        assert response.status_code == 404
        assert response.json()["code"] == "FEEDBACK_BLOCK_NOT_FOUND"


def test_feedback_scope_and_type_are_validated():
    with feedback_client() as client:
        response = client.post(
            "/api/feedback",
            headers=feedback_headers(),
            json={
                "scope": "global",
                "feedbackType": "inaccurate",
                "message": "类型不属于全局反馈",
            },
        )

        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_feedback_replays_the_same_request_and_rejects_key_reuse():
    with feedback_client() as client:
        body = {
            "scope": "global",
            "feedbackType": "feature",
            "message": "希望增加收藏功能",
            "pagePath": "/",
            "view": "home",
        }
        first = client.post(
            "/api/feedback",
            headers=feedback_headers("feedback-idempotency-key"),
            json=body,
        )
        replay = client.post(
            "/api/feedback",
            headers=feedback_headers("feedback-idempotency-key"),
            json=body,
        )
        conflict = client.post(
            "/api/feedback",
            headers=feedback_headers("feedback-idempotency-key"),
            json={**body, "message": "同一个键换成另一条反馈"},
        )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "FEEDBACK_IDEMPOTENCY_CONFLICT"
        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(UserFeedback)) == 1


def test_feedback_requires_an_idempotency_key():
    with feedback_client() as client:
        response = client.post(
            "/api/feedback",
            json={
                "scope": "global",
                "feedbackType": "bug",
                "message": "页面无法打开",
            },
        )

        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_feedback_rate_limit_is_user_scoped_and_persistent():
    with feedback_client() as client:
        body = {
            "scope": "global",
            "feedbackType": "experience",
            "message": "这是一条体验反馈",
            "pagePath": "/",
            "view": "home",
        }
        for index in range(FEEDBACK_PER_MINUTE_LIMIT):
            response = client.post(
                "/api/feedback",
                headers=feedback_headers(f"feedback-rate-limit-{index:02d}"),
                json=body,
            )
            assert response.status_code == 201

        limited = client.post(
            "/api/feedback",
            headers=feedback_headers("feedback-rate-limit-overflow"),
            json=body,
        )
        assert limited.status_code == 429
        assert limited.json()["code"] == "FEEDBACK_RATE_LIMITED"
        assert limited.json()["retryable"] is True
