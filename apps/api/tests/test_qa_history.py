import time

from fastapi.testclient import TestClient

from app.ai.local_adapter import LocalDemoAdapter
from app.demo_personas import LOCAL_DEMO_PASSWORD, LOCAL_DEMO_PERSONAS
from app.infrastructure.tables import QaSession
from app.main import create_app
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier

from test_vertical_slice import FakeAi, create_series, generate_and_pass


def test_qa_history_is_empty_then_returns_stably_ordered_threads(tmp_path):
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        FakeAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "attachments"),
    )
    with TestClient(app) as client:
        series = create_series(client)
        chapter = client.post(
            f"/api/chapters/{series['books'][0]['chapters'][0]['id']}/generate"
        ).json()
        section = generate_and_pass(client, chapter["sections"][0]["id"])
        block_id = section["content"]["blocks"][0]["id"]

        empty = client.get(f"/api/sections/{section['id']}/qa/history")
        assert empty.status_code == 200
        assert empty.json() == {
            "sectionId": section["id"],
            "lastThreadId": None,
            "threads": [],
            "truncated": False,
        }
        with client.app.state.sessions() as db:
            assert db.query(QaSession).count() == 0

        first = client.post(
            f"/api/sections/{section['id']}/ask",
            json={"blockId": block_id, "question": "第一个问题"},
        ).json()
        second = client.post(
            f"/api/sections/{section['id']}/ask",
            json={
                "blockId": block_id,
                "question": "第二个问题",
                "forceRelation": "new_question",
            },
        ).json()

        history = client.get(
            f"/api/sections/{section['id']}/qa/history"
        )
        assert history.status_code == 200
        payload = history.json()
        assert payload["lastThreadId"] == second["threadId"]
        assert [item["threadId"] for item in payload["threads"]] == [
            first["threadId"],
            second["threadId"],
        ]
        assert [
            message["role"]
            for thread in payload["threads"]
            for message in thread["messages"]
        ] == ["user", "assistant", "user", "assistant"]
        assert [
            message["content"]
            for thread in payload["threads"]
            for message in thread["messages"]
            if message["role"] == "user"
        ] == ["第一个问题", "第二个问题"]
        assert all(
            message["blockId"] == block_id and message["createdAt"]
            for thread in payload["threads"]
            for message in thread["messages"]
        )

        newest = second
        for index in range(9):
            newest = client.post(
                f"/api/sections/{section['id']}/ask",
                json={
                    "blockId": block_id,
                    "question": f"更多问题 {index}",
                    "forceRelation": "new_question",
                },
            ).json()
        bounded = client.get(
            f"/api/sections/{section['id']}/qa/history"
        ).json()
        assert bounded["truncated"] is True
        assert bounded["lastThreadId"] == newest["threadId"]
        assert len(bounded["threads"]) == 10
        assert first["threadId"] not in {
            thread["threadId"] for thread in bounded["threads"]
        }

        for index in range(10):
            client.post(
                f"/api/sections/{section['id']}/ask",
                json={
                    "blockId": block_id,
                    "question": f"更多追问 {index}",
                    "threadId": newest["threadId"],
                },
            )
        bounded_thread = client.get(
            f"/api/sections/{section['id']}/qa/history"
        ).json()
        latest_thread = next(
            thread
            for thread in bounded_thread["threads"]
            if thread["threadId"] == newest["threadId"]
        )
        assert bounded_thread["truncated"] is True
        assert len(latest_thread["messages"]) == 20
        assert [message["role"] for message in latest_thread["messages"]] == [
            role for _ in range(10) for role in ("user", "assistant")
        ]
        assert latest_thread["messages"][0]["content"] == "更多追问 0"


def test_qa_history_rejects_another_users_section(tmp_path):
    app = create_app(
        f"sqlite+pysqlite:///{tmp_path / 'qa-history-users.db'}",
        LocalDemoAdapter(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "user-attachments"),
        auth_mode="local",
        app_mode="development",
        runtime_settings_path=False,
    )

    def login(client, persona):
        client.cookies.clear()
        response = client.post(
            "/api/auth/local/login",
            json={
                "username": persona.username,
                "password": LOCAL_DEMO_PASSWORD,
            },
        )
        assert response.status_code == 200
        session = response.cookies["slow_session"]
        client.cookies.clear()
        return {
            "Cookie": f"slow_session={session}",
            "X-CSRF-Token": response.json()["csrfToken"],
        }

    def wait_for_owned_task(client, task_id, headers):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(
                f"/api/learning-tasks/{task_id}", headers=headers
            )
            assert response.status_code == 200
            if response.json()["status"] in {"succeeded", "failed"}:
                return response.json()
            time.sleep(0.01)
        raise AssertionError(f"task did not finish: {task_id}")

    with TestClient(app) as client:
        owner, other = LOCAL_DEMO_PERSONAS[:2]
        owner_headers = login(client, owner)
        owner_bootstrap = client.get(
            "/api/bootstrap", headers=owner_headers
        ).json()
        created = client.post(
            "/api/plans",
            headers={**owner_headers, "Idempotency-Key": "qa-history-owner"},
            json={
                "shelfId": owner_bootstrap["shelves"][0]["id"],
                "topic": "答疑历史隔离",
                "role": "学习者",
                "experience": "测试",
                "depth": "deep",
            },
        )
        assert created.status_code == 201
        task = wait_for_owned_task(
            client,
            created.json()["initializationTask"]["taskId"],
            owner_headers,
        )
        assert task["status"] == "succeeded"
        section_id = task["result"]["targetSectionId"]

        assert client.get(
            f"/api/sections/{section_id}/qa/history",
            headers=owner_headers,
        ).status_code == 200

        other_headers = login(client, other)
        denied = client.get(
            f"/api/sections/{section_id}/qa/history",
            headers=other_headers,
        )
        assert denied.status_code == 404
        assert denied.json()["code"] == "SECTION_NOT_FOUND"
