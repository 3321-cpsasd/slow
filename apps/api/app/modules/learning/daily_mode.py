from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import DailyModeEvent, UserDailyModeState, now


VALID_DAILY_MODES = {"fast", "slow"}
VALID_DURATIONS = {"1h", "3h", "6h", "today"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DailyModeService:
    """Sole writer for transient Fast/Slow state and its immutable audit log."""

    def __init__(self, db: Session, *, user_id: str, clock=now):
        self.db = db
        self.user_id = user_id
        self.clock = clock

    def current(self) -> dict:
        current = _utc(self.clock())
        state = self.db.get(UserDailyModeState, self.user_id)
        active = bool(state and _utc(state.expires_at) > current)
        return self._view(state, active=active, server_now=current)

    def activate(self, body, idempotency_key: str) -> dict:
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise AppError(
                "学习模式切换缺少有效的幂等键",
                code="DAILY_MODE_IDEMPOTENCY_INVALID",
                status=400,
            )
        try:
            user_timezone = ZoneInfo(body.timezone)
        except ZoneInfoNotFoundError as error:
            raise AppError(
                "无法识别当前时区",
                code="DAILY_MODE_TIMEZONE_INVALID",
                status=400,
            ) from error

        request_payload = {
            "dailyMode": body.daily_mode,
            "duration": body.duration,
            "timezone": body.timezone,
            "source": body.source,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing_event = self.db.scalar(
            select(DailyModeEvent).where(
                DailyModeEvent.user_id == self.user_id,
                DailyModeEvent.idempotency_key == key,
            )
        )
        if existing_event:
            if existing_event.request_hash != request_hash:
                raise AppError(
                    "幂等键已用于另一项学习模式切换",
                    code="DAILY_MODE_IDEMPOTENCY_CONFLICT",
                    status=409,
                )
            return self.current()

        activated_at = _utc(self.clock())
        expires_at = self._expiry(
            activated_at,
            duration=body.duration,
            user_timezone=user_timezone,
        )
        state = self.db.get(UserDailyModeState, self.user_id)
        previous_mode = state.daily_mode if state else None
        version = (state.version if state else 0) + 1
        if state:
            state.daily_mode = body.daily_mode
            state.duration = body.duration
            state.timezone = body.timezone
            state.activated_at = activated_at
            state.expires_at = expires_at
            state.version = version
            state.updated_at = activated_at
        else:
            state = UserDailyModeState(
                user_id=self.user_id,
                daily_mode=body.daily_mode,
                duration=body.duration,
                timezone=body.timezone,
                activated_at=activated_at,
                expires_at=expires_at,
                version=version,
                updated_at=activated_at,
            )
            self.db.add(state)
        event = DailyModeEvent(
            id=f"daily_mode_{uuid4().hex}",
            user_id=self.user_id,
            previous_mode=previous_mode,
            daily_mode=body.daily_mode,
            duration=body.duration,
            timezone=body.timezone,
            source=body.source,
            activated_at=activated_at,
            expires_at=expires_at,
            state_version=version,
            idempotency_key=key,
            request_hash=request_hash,
            created_at=activated_at,
        )
        self.db.add(event)
        self.db.commit()
        return self._view(state, active=True, server_now=activated_at)

    def activity_snapshot(self) -> dict | None:
        state = self.current()
        if not state["active"]:
            return None
        return {
            "dailyModeAtStart": state["dailyMode"],
            "dailyModeStateVersion": state["version"],
            "activityStartedAt": state["serverNow"],
        }

    @staticmethod
    def _expiry(
        activated_at: datetime,
        *,
        duration: str,
        user_timezone: ZoneInfo,
    ) -> datetime:
        if duration == "today":
            local = activated_at.astimezone(user_timezone)
            tomorrow = (local + timedelta(days=1)).date()
            return datetime.combine(
                tomorrow,
                datetime.min.time(),
                tzinfo=user_timezone,
            ).astimezone(timezone.utc)
        hours = {"1h": 1, "3h": 3, "6h": 6}.get(duration)
        if hours is None:
            raise AppError(
                "学习模式持续时间无效",
                code="DAILY_MODE_DURATION_INVALID",
                status=400,
            )
        return activated_at + timedelta(hours=hours)

    def _view(
        self,
        state: UserDailyModeState | None,
        *,
        active: bool,
        server_now: datetime,
    ) -> dict:
        return {
            "active": active,
            "dailyMode": state.daily_mode if active and state else None,
            "lastDailyMode": state.daily_mode if state else None,
            "duration": state.duration if active and state else None,
            "timezone": state.timezone if state else None,
            "activatedAt": (
                _utc(state.activated_at).isoformat() if active and state else None
            ),
            "expiresAt": (
                _utc(state.expires_at).isoformat() if active and state else None
            ),
            "version": state.version if state else 0,
            "serverNow": server_now.isoformat(),
        }
