from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import LearningRun, StudyActivityPulse, now
from ..library.context import ActiveLearningContextResolver


MEASUREMENT_RULE_VERSION = "study_time_v1"
MAX_PULSE_GAP = timedelta(seconds=30)
MAX_INTERVAL = timedelta(seconds=20)
EPISODE_GAP = timedelta(minutes=5)
KIND_PRECEDENCE = {
    "reading_thinking": 1,
    "ask_ai": 2,
    "verification_review": 3,
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class StudyActivityService:
    """Sole writer and deterministic projector for estimated study time."""

    def __init__(self, db: Session, *, user_id: str, clock=now):
        self.db = db
        self.user_id = user_id
        self.clock = clock
        self.contexts = ActiveLearningContextResolver(db)

    def heartbeat(self, body) -> dict:
        user_timezone = self._timezone(body.timezone)
        del user_timezone  # Validation and a durable snapshot are both intentional.
        received_at = _utc(self.clock())
        recent_count = self.db.scalar(
            select(func.count(StudyActivityPulse.id)).where(
                StudyActivityPulse.user_id == self.user_id,
                StudyActivityPulse.received_at >= received_at - timedelta(minutes=1),
            )
        ) or 0
        if recent_count >= 120:
            raise AppError(
                "学习时间记录过于频繁，请稍后重试",
                code="STUDY_ACTIVITY_RATE_LIMITED",
                status=429,
            )

        payload = body.model_dump(mode="json")
        request_hash = sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self.db.scalar(
            select(StudyActivityPulse).where(
                StudyActivityPulse.user_id == self.user_id,
                StudyActivityPulse.event_id == body.event_id,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise AppError(
                    "学习时间事件 ID 已用于不同内容",
                    code="STUDY_ACTIVITY_IDEMPOTENCY_CONFLICT",
                    status=409,
                )
            return {
                "accepted": False,
                "duplicated": True,
                "serverNow": _utc(existing.received_at).isoformat(),
                "measurementRuleVersion": existing.measurement_rule_version,
            }

        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=body.section_id,
        )
        learning_run_id = self.db.scalar(
            select(LearningRun.id).where(
                LearningRun.user_id == self.user_id,
                LearningRun.series_id == context.series.id,
                LearningRun.status == "active",
            )
        )
        self.db.add(
            StudyActivityPulse(
                id=f"study_pulse_{uuid4().hex}",
                user_id=self.user_id,
                event_id=body.event_id,
                client_session_id=body.client_session_id,
                client_sequence=body.client_sequence,
                activity_kind=body.activity_kind,
                section_id=body.section_id,
                learning_run_id=learning_run_id,
                timezone_snapshot=body.timezone,
                request_hash=request_hash,
                measurement_rule_version=MEASUREMENT_RULE_VERSION,
                received_at=received_at,
            )
        )
        self.db.commit()
        return {
            "accepted": True,
            "duplicated": False,
            "serverNow": received_at.isoformat(),
            "measurementRuleVersion": MEASUREMENT_RULE_VERSION,
        }

    def today(self, timezone_name: str) -> dict:
        user_timezone = self._timezone(timezone_name)
        current = _utc(self.clock())
        local_date = current.astimezone(user_timezone).date()
        window_start = datetime.combine(
            local_date,
            time.min,
            tzinfo=user_timezone,
        ).astimezone(timezone.utc)
        window_end = min(
            current,
            datetime.combine(
                local_date + timedelta(days=1),
                time.min,
                tzinfo=user_timezone,
            ).astimezone(timezone.utc),
        )
        pulses = list(
            self.db.scalars(
                select(StudyActivityPulse)
                .where(
                    StudyActivityPulse.user_id == self.user_id,
                    StudyActivityPulse.received_at >= window_start - MAX_PULSE_GAP,
                    StudyActivityPulse.received_at <= window_end,
                )
                .order_by(
                    StudyActivityPulse.client_session_id,
                    StudyActivityPulse.received_at,
                    StudyActivityPulse.id,
                )
            )
        )
        intervals = self._intervals(
            pulses,
            window_start=window_start,
            window_end=window_end,
        )
        segments = self._deduplicate(intervals)
        category_seconds = {kind: 0 for kind in KIND_PRECEDENCE}
        for segment in segments:
            category_seconds[segment["kind"]] += int(
                (segment["end"] - segment["start"]).total_seconds()
            )
        total_seconds = sum(category_seconds.values())
        episodes = self._episodes(segments)
        return {
            "date": local_date.isoformat(),
            "timezone": timezone_name,
            "totalSeconds": total_seconds,
            "categories": [
                {"activityKind": kind, "seconds": category_seconds[kind]}
                for kind in (
                    "reading_thinking",
                    "verification_review",
                    "ask_ai",
                )
            ],
            "episodes": episodes,
            "measurementRuleVersion": MEASUREMENT_RULE_VERSION,
            "estimated": True,
            "serverNow": current.isoformat(),
        }

    @staticmethod
    def _timezone(value: str) -> ZoneInfo:
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise AppError(
                "无法识别当前时区",
                code="STUDY_ACTIVITY_TIMEZONE_INVALID",
                status=400,
            ) from error

    @staticmethod
    def _intervals(
        pulses: list[StudyActivityPulse],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[dict]:
        by_session: dict[str, list[StudyActivityPulse]] = defaultdict(list)
        for pulse in pulses:
            by_session[pulse.client_session_id].append(pulse)
        intervals: list[dict] = []
        for session_pulses in by_session.values():
            for previous, current in zip(session_pulses, session_pulses[1:]):
                previous_at = _utc(previous.received_at)
                current_at = _utc(current.received_at)
                gap = current_at - previous_at
                if gap <= timedelta(0) or gap > MAX_PULSE_GAP:
                    continue
                start = max(previous_at, current_at - MAX_INTERVAL, window_start)
                end = min(current_at, window_end)
                if end <= start:
                    continue
                intervals.append({
                    "start": start,
                    "end": end,
                    "kind": previous.activity_kind,
                })
        return intervals

    @staticmethod
    def _deduplicate(intervals: list[dict]) -> list[dict]:
        events: dict[datetime, list[tuple[int, int]]] = defaultdict(list)
        for index, interval in enumerate(intervals):
            events[interval["start"]].append((1, index))
            events[interval["end"]].append((-1, index))
        active: set[int] = set()
        previous_at: datetime | None = None
        segments: list[dict] = []
        for at in sorted(events):
            if previous_at is not None and at > previous_at and active:
                kind = max(
                    (intervals[index]["kind"] for index in active),
                    key=lambda item: KIND_PRECEDENCE[item],
                )
                if segments and segments[-1]["kind"] == kind and segments[-1]["end"] == previous_at:
                    segments[-1]["end"] = at
                else:
                    segments.append({"start": previous_at, "end": at, "kind": kind})
            for operation, index in events[at]:
                if operation < 0:
                    active.discard(index)
            for operation, index in events[at]:
                if operation > 0:
                    active.add(index)
            previous_at = at
        return segments

    @staticmethod
    def _episodes(segments: list[dict]) -> list[dict]:
        episodes: list[dict] = []
        for segment in segments:
            seconds = int((segment["end"] - segment["start"]).total_seconds())
            if not episodes or segment["start"] - episodes[-1]["ended_at"] > EPISODE_GAP:
                episodes.append({
                    "started_at": segment["start"],
                    "ended_at": segment["end"],
                    "duration_seconds": seconds,
                })
                continue
            episodes[-1]["ended_at"] = segment["end"]
            episodes[-1]["duration_seconds"] += seconds
        return [
            {
                "startedAt": episode["started_at"].isoformat(),
                "endedAt": episode["ended_at"].isoformat(),
                "durationSeconds": episode["duration_seconds"],
            }
            for episode in episodes
        ]
