from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai.local_adapter import LocalDemoAdapter
from app.core.errors import AppError
from app.infrastructure.tables import Base, DailyModeEvent, User
from app.main import create_app
from app.modules.learning.daily_mode import DailyModeService
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


def body(
    daily_mode="fast",
    duration="1h",
    timezone_name="Asia/Shanghai",
    source="dialog",
):
    return SimpleNamespace(
        daily_mode=daily_mode,
        duration=duration,
        timezone=timezone_name,
        source=source,
    )


def database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all(
        [
            User(id="user_a", name="A"),
            User(id="user_b", name="B"),
        ]
    )
    db.commit()
    return db


def test_daily_mode_is_user_scoped_audited_and_idempotent():
    db = database()
    clock = Clock(datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc))
    service = DailyModeService(db, user_id="user_a", clock=clock)

    first = service.activate(body(), "mode-request-1")
    replay = service.activate(body(), "mode-request-1")
    other_user = DailyModeService(db, user_id="user_b", clock=clock).current()

    assert first["dailyMode"] == "fast"
    assert first["duration"] == "1h"
    assert first["version"] == 1
    assert replay["version"] == 1
    assert other_user["active"] is False
    assert len(db.scalars(select(DailyModeEvent)).all()) == 1

    with pytest.raises(AppError) as raised:
        service.activate(body(daily_mode="slow"), "mode-request-1")
    assert raised.value.code == "DAILY_MODE_IDEMPOTENCY_CONFLICT"


def test_today_uses_user_timezone_and_expiry_is_read_without_mutation():
    db = database()
    clock = Clock(datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc))
    service = DailyModeService(db, user_id="user_a", clock=clock)

    activated = service.activate(body(duration="today"), "mode-today")
    assert activated["expiresAt"] == "2026-08-09T16:00:00+00:00"

    clock.value = datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc)
    expired = service.current()
    assert expired["active"] is False
    assert expired["dailyMode"] is None
    assert expired["lastDailyMode"] == "fast"
    assert len(db.scalars(select(DailyModeEvent)).all()) == 1


def test_daily_mode_api_is_in_bootstrap_and_rejects_invalid_timezone(tmp_path):
    app = create_app(
        f"sqlite+pysqlite:///{tmp_path / 'daily-mode.db'}",
        LocalDemoAdapter(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="demo",
        app_mode="test",
    )
    with TestClient(app) as client:
        initial = client.get("/api/bootstrap")
        assert initial.status_code == 200
        assert initial.json()["dailyMode"]["active"] is False

        invalid = client.put(
            "/api/daily-mode",
            headers={"Idempotency-Key": "invalid-zone"},
            json={
                "dailyMode": "slow",
                "duration": "3h",
                "timezone": "Not/A_Timezone",
                "source": "dialog",
            },
        )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "DAILY_MODE_TIMEZONE_INVALID"

        updated = client.put(
            "/api/daily-mode",
            headers={"Idempotency-Key": "valid-mode"},
            json={
                "dailyMode": "slow",
                "duration": "3h",
                "timezone": "Asia/Shanghai",
                "source": "dialog",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["dailyMode"] == "slow"
        assert client.get("/api/bootstrap").json()["dailyMode"]["version"] == 1
