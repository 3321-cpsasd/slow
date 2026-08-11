from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.schemas import LearningPreferenceEvidenceCreate, PersonalPresentationAdopt
from app.core.errors import AppError
from app.infrastructure.tables import (
    Base,
    ContentVersion,
    LearningPreferenceEvidence,
    PersonalBlockPresentation,
    QaMessage,
    QaSession,
    User,
)
from app.main import create_app
from app.modules.preferences.service import (
    LearningPreferenceService,
    PersonalPresentationService,
)
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier

from test_vertical_slice import FakeAi, create_series, generate_and_pass


def database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([
        User(id="user_a", name="A"),
        User(id="user_b", name="B"),
        ContentVersion(
            id="content_section_1",
            section_id="section_1",
            version=1,
            blocks_json=json.dumps([{"id": "block_1", "kind": "text"}]),
            sources_json="[]",
            confidence="high",
            publication_status="published",
        ),
        ContentVersion(
            id="content_section_2",
            section_id="section_2",
            version=1,
            blocks_json=json.dumps([{"id": "block_1", "kind": "text"}]),
            sources_json="[]",
            confidence="high",
            publication_status="published",
        ),
    ])
    db.commit()
    return db


def evidence(event_id, *, section_id="section_1", style="worked_example", signal="requested", parent=None, custom=None):
    return LearningPreferenceEvidenceCreate(
        eventId=event_id,
        requestEventId=parent,
        sectionId=section_id,
        contentVersionId=f"content_{section_id}",
        blockId="block_1",
        blockKind="text",
        style=style,
        signal=signal,
        customInstruction=custom,
    )


def test_repeated_helpful_evidence_activates_only_across_multiple_sections():
    db = database()
    clock = lambda: datetime(2026, 8, 11, tzinfo=timezone.utc)
    service = LearningPreferenceService(db, "user_a", clock=clock)
    for index, section_id in enumerate(("section_1", "section_1", "section_2"), start=1):
        request_id = f"request_{index:02d}"
        service.record(evidence(request_id, section_id=section_id), shelf_id="shelf_1")
        result = service.record(
            evidence(f"helpful_{index:02d}", section_id=section_id, signal="helpful", parent=request_id),
            shelf_id="shelf_1",
        )

    example = next(item for item in result["dimensions"] if item["key"] == "example")
    assert example["positiveOutcomes"] == 3
    assert example["contextCount"] == 2
    assert example["active"] is True
    assert result["effectivePreferences"]["formatPreferences"] == ["worked_example"]


def test_custom_text_is_reduced_to_bounded_features_and_not_retained():
    db = database()
    service = LearningPreferenceService(db, "user_a")
    service.record(
        evidence(
            "custom_request_1",
            style="custom",
            custom="用生活里的快递网络来讲，少用术语",
        ),
        shelf_id="shelf_1",
    )
    row = db.scalar(select(LearningPreferenceEvidence))
    assert "快递" not in row.dimensions_json
    assert "生活" not in row.dimensions_json
    assert "example" in row.dimensions_json
    assert "plain_language" in row.dimensions_json
    assert not hasattr(row, "custom_instruction")


def test_feedback_cannot_reference_another_users_request():
    db = database()
    LearningPreferenceService(db, "user_a").record(
        evidence("request_private"),
        shelf_id="shelf_1",
    )
    with pytest.raises(AppError) as raised:
        LearningPreferenceService(db, "user_b").record(
            evidence("feedback_other", signal="helpful", parent="request_private"),
            shelf_id="shelf_1",
        )
    assert raised.value.code == "PREFERENCE_EVIDENCE_PARENT_NOT_FOUND"


def test_evidence_rejects_a_block_outside_the_published_content():
    db = database()
    invalid = evidence("request_invalid_block")
    invalid.block_id = "missing_block"
    with pytest.raises(AppError) as raised:
        LearningPreferenceService(db, "user_a").record(
            invalid,
            shelf_id="shelf_1",
        )
    assert raised.value.code == "PREFERENCE_BLOCK_NOT_FOUND"


def test_personal_presentation_is_bound_to_exact_answer_and_user():
    db = database()
    db.add_all([
        QaSession(
            id="qa_session_1",
            learning_run_id="run_1",
            section_id="section_1",
            user_id="user_a",
            content_version_id="content_section_1",
        ),
        QaMessage(
            id="answer_1",
            session_id="qa_session_1",
            thread_id="thread_1",
            block_id="block_1",
            role="assistant",
            content="第一种讲法",
        ),
        QaMessage(
            id="answer_2",
            session_id="qa_session_1",
            thread_id="thread_1",
            block_id="block_1",
            role="assistant",
            content="后续追问的回答",
        ),
    ])
    db.commit()
    body = PersonalPresentationAdopt(
        eventId="adopt_event_1",
        requestEventId="request_event_1",
        contentVersionId="content_section_1",
        blockId="block_1",
        blockKind="text",
        style="worked_example",
        threadId="thread_1",
        answerMessageId="answer_1",
    )

    override = PersonalPresentationService(db, "user_a").adopt(
        body,
        section_id="section_1",
    )
    assert override.source_qa_message_id == "answer_1"
    assert override.replacement_content == "第一种讲法"

    with pytest.raises(AppError) as raised:
        PersonalPresentationService(db, "user_b").adopt(
            body,
            section_id="section_1",
        )
    assert raised.value.code == "QA_SESSION_NOT_FOUND"


def test_preference_api_adopts_and_restores_exact_answer(tmp_path):
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
        content_id = section["content"]["id"]
        block = section["content"]["blocks"][0]
        request_event_id = "preference_request_1"

        requested = client.post(
            "/api/learning-preferences/evidence",
            json={
                "eventId": request_event_id,
                "sectionId": section["id"],
                "contentVersionId": content_id,
                "blockId": block["id"],
                "blockKind": block["kind"],
                "style": "worked_example",
                "signal": "requested",
            },
        )
        assert requested.status_code == 202

        first_answer = client.post(
            f"/api/sections/{section['id']}/ask",
            json={"blockId": block["id"], "question": "请举一个例子"},
        ).json()
        client.post(
            f"/api/sections/{section['id']}/ask",
            json={
                "blockId": block["id"],
                "question": "继续追问",
                "threadId": first_answer["threadId"],
            },
        )
        adopted = client.post(
            f"/api/sections/{section['id']}/personal-presentation",
            json={
                "eventId": "preference_adopt_1",
                "requestEventId": request_event_id,
                "contentVersionId": content_id,
                "blockId": block["id"],
                "blockKind": block["kind"],
                "style": "worked_example",
                "threadId": first_answer["threadId"],
                "answerMessageId": first_answer["answerMessageId"],
            },
        )
        assert adopted.status_code == 201

        with client.app.state.sessions() as db:
            override = db.scalar(select(PersonalBlockPresentation))
            assert override.source_qa_message_id == first_answer["answerMessageId"]
            expected_content = db.get(QaMessage, first_answer["answerMessageId"]).content
        refreshed = client.get(f"/api/sections/{section['id']}").json()
        refreshed_block = next(
            item for item in refreshed["content"]["blocks"]
            if item["id"] == block["id"]
        )
        assert refreshed_block["personalPresentation"]["content"] == expected_content

        restored = client.delete(
            f"/api/sections/{section['id']}/personal-presentation/{block['id']}",
            params={"contentVersionId": content_id},
        )
        assert restored.status_code == 204
        after_restore = client.get(f"/api/sections/{section['id']}").json()
        restored_block = next(
            item for item in after_restore["content"]["blocks"]
            if item["id"] == block["id"]
        )
        assert "personalPresentation" not in restored_block
