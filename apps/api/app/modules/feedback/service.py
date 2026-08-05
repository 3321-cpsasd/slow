import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...api.schemas import FeedbackCreate
from ...auth.context import UserScope
from ...core.errors import AppError
from ...infrastructure.tables import (
    Book,
    Chapter,
    ContentVersion,
    Section,
    Series,
    Shelf,
    UserFeedback,
)


FEEDBACK_PER_MINUTE_LIMIT = 8
FEEDBACK_PER_DAY_LIMIT = 100


class FeedbackService:
    """Append-only writer for global and version-bound content feedback."""

    def __init__(self, db: Session, scope: UserScope, *, source_mode: str):
        self.db = db
        self.scope = scope
        self.source_mode = source_mode

    def submit(self, body: FeedbackCreate, idempotency_key: str) -> dict:
        request_key = idempotency_key.strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "反馈请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        page_path = self._normalize_page_path(body.page_path)
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "scope": body.scope,
                    "feedbackType": body.feedback_type,
                    "message": body.message,
                    "pagePath": page_path,
                    "view": body.view,
                    "sectionId": body.section_id,
                    "contentVersionId": body.content_version_id,
                    "blockId": body.block_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        replay = self._find_replay(request_key)
        if replay:
            return self._replay(replay, request_hash)

        block_snapshot_hash = ""
        if body.scope == "content_block":
            content = self.db.scalar(
                select(ContentVersion)
                .join(Section, Section.id == ContentVersion.section_id)
                .join(Chapter, Chapter.id == Section.chapter_id)
                .join(Book, Book.id == Chapter.book_id)
                .join(Series, Series.id == Book.series_id)
                .join(Shelf, Shelf.id == Series.shelf_id)
                .where(
                    ContentVersion.id == body.content_version_id,
                    ContentVersion.section_id == body.section_id,
                    Shelf.user_id == self.scope.user_id,
                )
            )
            if not content:
                raise AppError(
                    "找不到可反馈的正文版本",
                    code="FEEDBACK_TARGET_NOT_FOUND",
                    status=404,
                )
            try:
                blocks = json.loads(content.blocks_json)
            except (TypeError, ValueError):
                blocks = []
            block = next(
                (
                    item for item in blocks
                    if isinstance(item, dict) and str(item.get("id", "")) == body.block_id
                ),
                None,
            )
            if block is None:
                raise AppError(
                    "该段落不属于当前正文版本",
                    code="FEEDBACK_BLOCK_NOT_FOUND",
                    status=404,
                )
            block_snapshot_hash = hashlib.sha256(
                json.dumps(
                    block,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()

        self._enforce_rate_limit()
        item = UserFeedback(
            id=f"feedback_{uuid4().hex}",
            user_id=self.scope.user_id,
            scope=body.scope,
            feedback_type=body.feedback_type,
            message=body.message,
            page_path=page_path,
            view=body.view,
            section_id=body.section_id,
            content_version_id=body.content_version_id,
            block_id=body.block_id,
            block_snapshot_hash=block_snapshot_hash,
            source_mode=self.source_mode,
            schema_version="feedback_v1",
            idempotency_key=request_key,
            request_hash=request_hash,
            context_json=json.dumps(
                {"pagePath": page_path, "view": body.view},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self.db.add(item)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self._find_replay(request_key)
            if replay:
                return self._replay(replay, request_hash)
            raise
        return self._receipt(item)

    def _find_replay(self, idempotency_key: str) -> UserFeedback | None:
        return self.db.scalar(
            select(UserFeedback).where(
                UserFeedback.user_id == self.scope.user_id,
                UserFeedback.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _replay(item: UserFeedback, request_hash: str) -> dict:
        if item.request_hash != request_hash:
            raise AppError(
                "反馈请求标识已用于其他内容",
                code="FEEDBACK_IDEMPOTENCY_CONFLICT",
                status=409,
            )
        return FeedbackService._receipt(item)

    def _enforce_rate_limit(self) -> None:
        observed_at = datetime.now(timezone.utc)
        minute_count = self.db.scalar(
            select(func.count())
            .select_from(UserFeedback)
            .where(
                UserFeedback.user_id == self.scope.user_id,
                UserFeedback.created_at >= observed_at - timedelta(minutes=1),
            )
        ) or 0
        day_count = self.db.scalar(
            select(func.count())
            .select_from(UserFeedback)
            .where(
                UserFeedback.user_id == self.scope.user_id,
                UserFeedback.created_at >= observed_at - timedelta(days=1),
            )
        ) or 0
        if minute_count >= FEEDBACK_PER_MINUTE_LIMIT or day_count >= FEEDBACK_PER_DAY_LIMIT:
            raise AppError(
                "反馈提交得太频繁，请稍后再试",
                code="FEEDBACK_RATE_LIMITED",
                status=429,
                retryable=True,
            )

    @staticmethod
    def _normalize_page_path(value: str) -> str:
        candidate = (value or "/").strip()
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or "\x00" in candidate
        ):
            return "/"
        path = urlsplit(candidate).path or "/"
        return path[:500]

    @staticmethod
    def _receipt(item: UserFeedback) -> dict:
        return {
            "id": item.id,
            "status": "received",
            "scope": item.scope,
            "createdAt": item.created_at.isoformat(),
        }
