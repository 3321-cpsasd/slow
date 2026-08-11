from datetime import timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...api.schemas import ProductEventCreate
from ...core.errors import AppError
from ...infrastructure.tables import ProductEvent, now


_PROPERTY_RULES: dict[str, dict[str, set[str] | type]] = {
    "home_viewed": {},
    "shelf_viewed": {},
    "learning_viewed": {},
    "profile_viewed": {},
    "section_viewed": {},
    "quiz_viewed": {},
    "feedback_opened": {"scope": {"global", "content_block"}},
    "explanation_style_requested": {
        "style": {"worked_example", "diagram", "analogy", "derivation", "precise", "concise", "custom"},
        "blockKind": {"text", "bullet_list", "ordered_steps", "diagram", "table", "code", "formula"},
    },
    "explanation_style_feedback": {
        "style": {"worked_example", "diagram", "analogy", "derivation", "precise", "concise", "custom"},
        "helpful": bool,
    },
    "explanation_style_remembered": {
        "style": {"worked_example", "diagram", "analogy", "derivation", "precise", "concise"},
    },
    "active_reading_60s": {"seconds": int},
    "frontend_error": {
        "kind": {"window_error", "unhandled_rejection", "render_error"},
    },
}

_EXPECTED_CONTEXT = {
    "home_viewed": ("home", {""}),
    "shelf_viewed": ("shelf", {"shelf"}),
    "learning_viewed": ("learn", {"series", "section"}),
    "profile_viewed": ("profile", {""}),
    "section_viewed": ("learn", {"section"}),
    "quiz_viewed": ("learn", {"section"}),
    "feedback_opened": (None, {"", "section"}),
    "explanation_style_requested": ("learn", {"section"}),
    "explanation_style_feedback": ("learn", {"section"}),
    "explanation_style_remembered": ("learn", {"section"}),
    "active_reading_60s": ("learn", {"section"}),
    "frontend_error": (None, {""}),
}


class ProductEventService:
    """Validate and append product events; never treats them as learning evidence."""

    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id

    def append(self, events: list[ProductEventCreate]) -> dict:
        received_at = now()
        recent_count = self.db.scalar(
            select(func.count(ProductEvent.id)).where(
                ProductEvent.user_id == self.user_id,
                ProductEvent.received_at >= received_at - timedelta(minutes=1),
            )
        ) or 0
        if recent_count + len(events) > 240:
            raise AppError(
                "埋点写入过于频繁，请稍后重试",
                code="PRODUCT_EVENT_RATE_LIMITED",
                status=429,
            )

        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise AppError(
                "同一批次包含重复事件 ID",
                code="PRODUCT_EVENT_DUPLICATE_IN_BATCH",
                status=409,
            )
        existing = {
            row.event_id: row
            for row in self.db.scalars(
                select(ProductEvent).where(
                    ProductEvent.user_id == self.user_id,
                    ProductEvent.event_id.in_(event_ids),
                )
            )
        }

        accepted = 0
        duplicated = 0
        for event in events:
            self._validate(event)
            payload = self._canonical_payload(event)
            request_hash = sha256(payload.encode("utf-8")).hexdigest()
            prior = existing.get(event.event_id)
            if prior:
                if prior.request_hash != request_hash:
                    raise AppError(
                        "事件 ID 已被用于不同内容",
                        code="PRODUCT_EVENT_IDEMPOTENCY_CONFLICT",
                        status=409,
                    )
                duplicated += 1
                continue

            occurred_at = event.occurred_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            if (
                occurred_at < received_at - timedelta(hours=24)
                or occurred_at > received_at + timedelta(minutes=5)
            ):
                occurred_at = received_at
            self.db.add(
                ProductEvent(
                    id=f"product_event_{uuid4().hex}",
                    user_id=self.user_id,
                    event_id=event.event_id,
                    session_id=event.session_id,
                    event_name=event.event_name,
                    page_path=event.page_path,
                    view=event.view,
                    entity_type=event.entity_type,
                    entity_id=event.entity_id,
                    properties_json=json.dumps(
                        event.properties,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    request_hash=request_hash,
                    occurred_at=occurred_at,
                    received_at=received_at,
                )
            )
            accepted += 1
        self.db.commit()
        return {"accepted": accepted, "duplicated": duplicated}

    @staticmethod
    def _canonical_payload(event: ProductEventCreate) -> str:
        return json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _validate(event: ProductEventCreate) -> None:
        rules = _PROPERTY_RULES[event.event_name]
        if set(event.properties) != set(rules):
            raise AppError(
                "事件属性不符合白名单",
                code="PRODUCT_EVENT_PROPERTIES_INVALID",
                status=400,
            )
        for key, rule in rules.items():
            value = event.properties[key]
            if isinstance(rule, set) and value not in rule:
                raise AppError(
                    "事件属性值不符合白名单",
                    code="PRODUCT_EVENT_PROPERTIES_INVALID",
                    status=400,
                )
            if isinstance(rule, type) and type(value) is not rule:
                raise AppError(
                    "事件属性类型不符合白名单",
                    code="PRODUCT_EVENT_PROPERTIES_INVALID",
                    status=400,
                )
        if event.event_name == "active_reading_60s" and event.properties["seconds"] != 60:
            raise AppError(
                "有效阅读事件只能按 60 秒上报",
                code="PRODUCT_EVENT_PROPERTIES_INVALID",
                status=400,
            )

        expected_view, entity_types = _EXPECTED_CONTEXT[event.event_name]
        if expected_view is not None and event.view != expected_view:
            raise AppError(
                "事件页面上下文不一致",
                code="PRODUCT_EVENT_CONTEXT_INVALID",
                status=400,
            )
        if event.entity_type not in entity_types:
            raise AppError(
                "事件实体上下文不一致",
                code="PRODUCT_EVENT_CONTEXT_INVALID",
                status=400,
            )
        if bool(event.entity_type) != bool(event.entity_id):
            raise AppError(
                "事件实体类型与 ID 必须同时提供",
                code="PRODUCT_EVENT_CONTEXT_INVALID",
                status=400,
            )
