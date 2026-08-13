from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.local_adapter import LocalDemoAdapter
from app.api.schemas import StudyActivityHeartbeat
from app.core.errors import AppError
from app.infrastructure.tables import (
    Book,
    Chapter,
    LearningPlan,
    LearningRun,
    Section,
    Series,
    Shelf,
    StudyActivityPulse,
    User,
)
from app.main import create_app
from app.modules.learning.study_activity import StudyActivityService
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


def make_app(tmp_path):
    return create_app(
        f"sqlite+pysqlite:///{tmp_path / 'study-time.db'}",
        LocalDemoAdapter(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="demo",
        app_mode="test",
    )


def heartbeat(event_id: str, section_id: str, *, sequence: int = 0, kind: str = "reading_thinking"):
    return {
        "eventId": event_id,
        "clientSessionId": "study_session_123456",
        "clientSequence": sequence,
        "activityKind": kind,
        "sectionId": section_id,
        "timezone": "Asia/Shanghai",
    }


def create_section(db) -> str:
    shelf = db.scalar(select(Shelf))
    assert shelf
    plan = LearningPlan(
        id="study_plan",
        shelf_id=shelf.id,
        topic="Study time",
        role="Learner",
        experience="Beginner",
        depth="overview",
        confidence="demo",
    )
    series = Series(
        id="study_series",
        plan_id=plan.id,
        shelf_id=shelf.id,
        title="Study time series",
        rationale="Test",
    )
    book = Book(
        id="study_book",
        series_id=series.id,
        shelf_id=shelf.id,
        position=1,
        title="Study time book",
        topic="Study time",
        description="Test",
        estimated_minutes=60,
    )
    chapter = Chapter(
        id="study_chapter",
        book_id=book.id,
        position=1,
        title="Study time chapter",
        objective="Test",
    )
    section = Section(
        id="study_section",
        chapter_id=chapter.id,
        position=1,
        title="Study time section",
        question="How long?",
        objectives_json='["measure"]',
    )
    db.add(plan)
    db.commit()
    db.add(series)
    db.commit()
    db.add(book)
    db.commit()
    db.add(chapter)
    db.commit()
    db.add(section)
    db.commit()
    db.add(LearningRun(
        id="study_run",
        user_id=shelf.user_id,
        series_id=series.id,
    ))
    db.commit()
    return section.id


def test_heartbeat_is_idempotent_and_summary_rebuilds_categories_and_timeline(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/bootstrap").status_code == 200
        with client.app.state.sessions() as db:
            section_id = create_section(db)

        first = client.post(
            "/api/study-activity/heartbeat",
            json=heartbeat("study_evt_0001", section_id),
        )
        replay = client.post(
            "/api/study-activity/heartbeat",
            json=heartbeat("study_evt_0001", section_id),
        )
        second = client.post(
            "/api/study-activity/heartbeat",
            json=heartbeat("study_evt_0002", section_id, sequence=1),
        )
        assert first.status_code == 202
        assert first.json()["accepted"] is True
        assert replay.json()["duplicated"] is True
        assert second.status_code == 202

        base = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
        with client.app.state.sessions() as db:
            pulses = list(
                db.scalars(
                    select(StudyActivityPulse).order_by(StudyActivityPulse.event_id)
                )
            )
            pulses[0].received_at = base
            pulses[1].received_at = base + timedelta(seconds=15)
            db.commit()
            summary = StudyActivityService(
                db,
                user_id=pulses[0].user_id,
                clock=Clock(base + timedelta(seconds=15)),
            ).today("Asia/Shanghai")

        assert summary["totalSeconds"] == 15
        assert summary["categories"] == [
            {"activityKind": "reading_thinking", "seconds": 15},
            {"activityKind": "verification_review", "seconds": 0},
            {"activityKind": "ask_ai", "seconds": 0},
        ]
        assert len(summary["episodes"]) == 1
        assert summary["episodes"][0]["durationSeconds"] == 15
        assert summary["estimated"] is True


def test_projection_deduplicates_tabs_and_prefers_assessment_activity():
    base = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    segments = StudyActivityService._deduplicate([
        {
            "start": base,
            "end": base + timedelta(seconds=20),
            "kind": "reading_thinking",
        },
        {
            "start": base + timedelta(seconds=5),
            "end": base + timedelta(seconds=15),
            "kind": "ask_ai",
        },
        {
            "start": base + timedelta(seconds=8),
            "end": base + timedelta(seconds=12),
            "kind": "verification_review",
        },
    ])

    assert sum(int((item["end"] - item["start"]).total_seconds()) for item in segments) == 20
    assert [item["kind"] for item in segments] == [
        "reading_thinking",
        "ask_ai",
        "verification_review",
        "ask_ai",
        "reading_thinking",
    ]


def test_heartbeat_rejects_invalid_timezone_and_cross_user_section(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/bootstrap").status_code == 200
        with client.app.state.sessions() as db:
            section_id = create_section(db)
            db.add(User(id="study_other_user", name="Other"))
            db.commit()
            body = StudyActivityHeartbeat.model_validate(
                heartbeat("study_evt_0003", section_id)
            )
            with pytest.raises(AppError) as missing:
                StudyActivityService(
                    db,
                    user_id="study_other_user",
                ).heartbeat(body)
            assert missing.value.code == "SECTION_NOT_FOUND"

        invalid = heartbeat("study_evt_0004", section_id)
        invalid["timezone"] = "Not/A_Timezone"
        response = client.post("/api/study-activity/heartbeat", json=invalid)
        assert response.status_code == 400
        assert response.json()["code"] == "STUDY_ACTIVITY_TIMEZONE_INVALID"
