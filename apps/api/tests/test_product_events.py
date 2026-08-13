from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.local_adapter import LocalDemoAdapter
from app.auth.password import PasswordCredentialService
from app.infrastructure.tables import ProductEvent
from app.main import create_app
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


def make_client(tmp_path):
    app = create_app(
        f"sqlite+pysqlite:///{tmp_path / 'events.db'}",
        ai=LocalDemoAdapter(),
        source_verifier=AcceptingSourceVerifier(),
        attachment_storage=LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="password",
        app_mode="production",
        runtime_settings_path=False,
    )
    return TestClient(app, base_url="https://testserver")


def login(client: TestClient) -> str:
    with client.app.state.sessions() as db:
        PasswordCredentialService(db).create_account(
            username="event-user",
            display_name="埋点测试用户",
            password="Event-Test-Password-2026",
        )
    response = client.post(
        "/api/auth/password/login",
        json={"username": "event-user", "password": "Event-Test-Password-2026"},
    )
    assert response.status_code == 200
    return response.json()["csrfToken"]


def event_payload(**overrides):
    event = {
        "eventId": "evt_12345678",
        "sessionId": "session_12345678",
        "eventName": "section_viewed",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "pagePath": "/learn?secret=removed",
        "view": "learn",
        "entityType": "section",
        "entityId": "sec_1_1",
        "properties": {},
    }
    event.update(overrides)
    return {"events": [event]}


def accept_privacy(client: TestClient, csrf: str):
    response = client.post(
        "/api/privacy/consent",
        headers={"X-CSRF-Token": csrf},
        json={"privacyAccepted": True, "trialAccepted": True},
    )
    assert response.status_code == 200


def test_product_events_require_consent_and_are_idempotent(tmp_path):
    with make_client(tmp_path) as client:
        csrf = login(client)
        headers = {"X-CSRF-Token": csrf}
        payload = event_payload()

        blocked = client.post("/api/events/batch", headers=headers, json=payload)
        assert blocked.status_code == 428
        assert blocked.json()["code"] == "PRIVACY_CONSENT_REQUIRED"

        accept_privacy(client, csrf)
        created = client.post("/api/events/batch", headers=headers, json=payload)
        assert created.status_code == 202
        assert created.json() == {"accepted": 1, "duplicated": 0}

        replay = client.post("/api/events/batch", headers=headers, json=payload)
        assert replay.status_code == 202
        assert replay.json() == {"accepted": 0, "duplicated": 1}

        with client.app.state.sessions() as db:
            events = db.scalars(select(ProductEvent)).all()
            assert len(events) == 1
            assert events[0].event_name == "section_viewed"
            assert events[0].page_path == "/learn"
            assert events[0].entity_id == "sec_1_1"
            assert json.loads(events[0].properties_json) == {}


def test_product_events_reject_arbitrary_text_and_inconsistent_context(tmp_path):
    with make_client(tmp_path) as client:
        csrf = login(client)
        accept_privacy(client, csrf)
        headers = {"X-CSRF-Token": csrf}

        sensitive = client.post(
            "/api/events/batch",
            headers=headers,
            json=event_payload(properties={"answer": "不应被采集"}),
        )
        assert sensitive.status_code == 400
        assert sensitive.json()["code"] == "PRODUCT_EVENT_PROPERTIES_INVALID"

        wrong_context = client.post(
            "/api/events/batch",
            headers=headers,
            json=event_payload(eventId="evt_87654321", view="profile"),
        )
        assert wrong_context.status_code == 400
        assert wrong_context.json()["code"] == "PRODUCT_EVENT_CONTEXT_INVALID"

        free_form_error = client.post(
            "/api/events/batch",
            headers=headers,
            json=event_payload(
                eventId="evt_error_1234",
                eventName="frontend_error",
                view="learn",
                entityType="",
                entityId="",
                properties={"kind": "window_error", "message": "private note"},
            ),
        )
        assert free_form_error.status_code == 400
        assert free_form_error.json()["code"] == "PRODUCT_EVENT_PROPERTIES_INVALID"


def test_global_feedback_event_accepts_knowledge_view(tmp_path):
    with make_client(tmp_path) as client:
        csrf = login(client)
        accept_privacy(client, csrf)

        response = client.post(
            "/api/events/batch",
            headers={"X-CSRF-Token": csrf},
            json=event_payload(
                eventId="evt_knowledge_feedback",
                eventName="feedback_opened",
                pagePath="/knowledge",
                view="knowledge",
                entityType="",
                entityId="",
                properties={"scope": "global"},
            ),
        )

        assert response.status_code == 202


def test_review_center_event_accepts_review_view(tmp_path):
    with make_client(tmp_path) as client:
        csrf = login(client)
        accept_privacy(client, csrf)

        response = client.post(
            "/api/events/batch",
            headers={"X-CSRF-Token": csrf},
            json=event_payload(
                eventId="evt_review_center_view",
                eventName="review_center_viewed",
                pagePath="/review",
                view="review",
                entityType="",
                entityId="",
                properties={},
            ),
        )

        assert response.status_code == 202


def test_explanation_style_events_accept_only_bounded_preference_evidence(tmp_path):
    with make_client(tmp_path) as client:
        csrf = login(client)
        accept_privacy(client, csrf)
        headers = {"X-CSRF-Token": csrf}

        requested = client.post(
            "/api/events/batch",
            headers=headers,
            json=event_payload(
                eventId="evt_style_request_1",
                eventName="explanation_style_requested",
                properties={"style": "custom", "blockKind": "text"},
            ),
        )
        assert requested.status_code == 202

        custom_feedback = client.post(
            "/api/events/batch",
            headers=headers,
            json=event_payload(
                eventId="evt_style_feedback_custom",
                eventName="explanation_style_feedback",
                properties={"style": "custom", "helpful": True},
            ),
        )
        assert custom_feedback.status_code == 202

        rejected = client.post(
            "/api/events/batch",
            headers=headers,
            json=event_payload(
                eventId="evt_style_request_2",
                eventName="explanation_style_feedback",
                properties={"style": "diagram", "helpful": True, "answer": "私密内容"},
            ),
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "PRODUCT_EVENT_PROPERTIES_INVALID"
