import json
from urllib.parse import unquote
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ArtifactAttachment,
    ArtifactSubmission,
    Book,
    BookCapstone,
    Chapter,
    ChapterPractice,
    now,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _timestamp(value):
    return value.isoformat() if value else None


class ArtifactService:
    """Own chapter practices, book capstones, and their attachment boundary."""

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        contexts,
        progress,
        artifact_progress,
        attachment_storage,
    ):
        self.db = db
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress
        self.artifact_progress = artifact_progress
        self.attachment_storage = attachment_storage

    def chapter_practice(self, chapter_id: str) -> dict:
        self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id)
        practice = self.db.scalar(
            select(ChapterPractice).where(
                ChapterPractice.chapter_id == chapter_id
            )
        )
        if not practice:
            raise AppError(
                "请先生成本章",
                code="PRACTICE_NOT_GENERATED",
                status=404,
            )
        return self.practice_view(practice)

    def upload_chapter_practice_attachment(
        self,
        chapter_id: str,
        filename: str,
        media_type: str,
        data: bytes,
    ) -> dict:
        context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        run = self.progress.active_run(context.series.id)
        practice = self.db.scalar(
            select(ChapterPractice).where(
                ChapterPractice.chapter_id == chapter_id
            )
        )
        if not practice:
            raise AppError(
                "章末实践不存在", code="PRACTICE_NOT_FOUND", status=404
            )
        if self.practice_progress(practice).status == "locked":
            raise AppError(
                "完成本章后才可上传附件", code="PRACTICE_LOCKED", status=403
            )
        return self._upload_attachment(
            run.id,
            "chapter_practice",
            practice.id,
            filename,
            media_type,
            data,
        )

    def submit_chapter_practice(
        self,
        chapter_id: str,
        content: dict,
        attachment_ids: list[str],
    ) -> dict:
        context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        run = self.progress.active_run(context.series.id)
        practice = self.db.scalar(
            select(ChapterPractice).where(
                ChapterPractice.chapter_id == chapter_id
            )
        )
        if not practice:
            raise AppError(
                "章末实践不存在", code="PRACTICE_NOT_FOUND", status=404
            )
        progress = self.practice_progress(practice)
        if progress.status == "locked":
            raise AppError(
                "完成本章后才可提交实践", code="PRACTICE_LOCKED", status=403
            )
        if not content:
            raise AppError("实践提交不能为空", code="PRACTICE_EMPTY")
        attachments = self._validated_attachments(
            run.id,
            "chapter_practice",
            practice.id,
            attachment_ids,
        )
        attachment_ids = [item.id for item in attachments]
        self.db.add(
            ArtifactSubmission(
                id=_uid("artifact_submission"),
                learning_run_id=run.id,
                user_id=self.user_id,
                target_type="chapter_practice",
                target_id=practice.id,
                content_json=_dump(content),
                attachment_ids_json=_dump(attachment_ids),
            )
        )
        progress.submission_json = _dump(
            {"content": content, "attachmentIds": attachment_ids}
        )
        progress.status = "completed"
        progress.updated_at = now()
        self.db.commit()
        return self.practice_view(practice)

    def practice_progress(self, practice):
        chapter = self.db.get(Chapter, practice.chapter_id)
        book = self.db.get(Book, chapter.book_id)
        run = self.progress.active_run(book.series_id)
        return self.artifact_progress.for_target(
            learning_run_id=run.id,
            target_type="chapter_practice",
            target_id=practice.id,
        )

    def practice_view(self, practice) -> dict:
        progress = self.practice_progress(practice)
        attachments = self._attachments(
            progress.learning_run_id,
            "chapter_practice",
            practice.id,
        )
        return {
            "id": practice.id,
            "title": practice.title,
            "instructions": _load(practice.instructions_json, {}),
            "submission": _load(progress.submission_json, {}),
            "attachments": attachments,
            "evidenceMode": (
                "file_attachment" if attachments else "structured_only_legacy"
            ),
            "status": progress.status,
        }

    def book_capstone(self, book_id: str) -> dict:
        self.contexts.resolve_book(user_id=self.user_id, book_id=book_id)
        capstone = self.db.scalar(
            select(BookCapstone).where(BookCapstone.book_id == book_id)
        )
        if not capstone:
            raise AppError(
                "全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404
            )
        return self.capstone_view(capstone)

    def upload_book_capstone_attachment(
        self,
        book_id: str,
        filename: str,
        media_type: str,
        data: bytes,
    ) -> dict:
        context = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        )
        run = self.progress.active_run(context.series.id)
        capstone = self.db.scalar(
            select(BookCapstone).where(BookCapstone.book_id == book_id)
        )
        if not capstone:
            raise AppError(
                "全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404
            )
        if self.capstone_progress(capstone).status == "locked":
            raise AppError(
                "完成本书正文后才可上传附件",
                code="CAPSTONE_LOCKED",
                status=403,
            )
        return self._upload_attachment(
            run.id,
            "book_capstone",
            capstone.id,
            filename,
            media_type,
            data,
        )

    def submit_book_capstone(
        self,
        book_id: str,
        content: dict,
        attachment_ids: list[str],
    ) -> dict:
        context = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        )
        run = self.progress.active_run(context.series.id)
        capstone = self.db.scalar(
            select(BookCapstone).where(BookCapstone.book_id == book_id)
        )
        if not capstone:
            raise AppError(
                "全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404
            )
        progress = self.capstone_progress(capstone)
        if progress.status == "locked":
            raise AppError(
                "完成本书后才可提交大作业",
                code="CAPSTONE_LOCKED",
                status=403,
            )
        if not content:
            raise AppError("大作业提交不能为空", code="CAPSTONE_EMPTY")
        attachments = self._validated_attachments(
            run.id,
            "book_capstone",
            capstone.id,
            attachment_ids,
        )
        attachment_ids = [item.id for item in attachments]
        self.db.add(
            ArtifactSubmission(
                id=_uid("artifact_submission"),
                learning_run_id=run.id,
                user_id=self.user_id,
                target_type="book_capstone",
                target_id=capstone.id,
                content_json=_dump(content),
                attachment_ids_json=_dump(attachment_ids),
            )
        )
        progress.submission_json = _dump(
            {"content": content, "attachmentIds": attachment_ids}
        )
        progress.status = "completed"
        progress.updated_at = now()
        self.db.commit()
        return self.capstone_view(capstone)

    def capstone_progress(self, capstone):
        book = self.db.get(Book, capstone.book_id)
        run = self.progress.active_run(book.series_id)
        return self.artifact_progress.for_target(
            learning_run_id=run.id,
            target_type="book_capstone",
            target_id=capstone.id,
        )

    def capstone_view(self, capstone) -> dict:
        progress = self.capstone_progress(capstone)
        attachments = self._attachments(
            progress.learning_run_id,
            "book_capstone",
            capstone.id,
        )
        return {
            "id": capstone.id,
            "title": capstone.title,
            "brief": _load(capstone.brief_json, {}),
            "submission": _load(progress.submission_json, {}),
            "attachments": attachments,
            "evidenceMode": (
                "file_attachment" if attachments else "structured_only_legacy"
            ),
            "status": progress.status,
        }

    def attachment(self, attachment_id: str):
        item = self.db.scalar(
            select(ArtifactAttachment).where(
                ArtifactAttachment.id == attachment_id,
                ArtifactAttachment.user_id == self.user_id,
            )
        )
        if not item:
            raise AppError(
                "附件不存在", code="ATTACHMENT_NOT_FOUND", status=404
            )
        if not self.attachment_storage:
            raise AppError(
                "附件存储未配置",
                code="ATTACHMENT_STORAGE_UNAVAILABLE",
                status=503,
            )
        path = self.attachment_storage.resolve(item.object_key)
        if not path.is_file():
            raise AppError(
                "附件对象缺失", code="ATTACHMENT_OBJECT_MISSING", status=410
            )
        return item, path

    def _upload_attachment(
        self,
        learning_run_id,
        target_type,
        target_id,
        filename,
        media_type,
        data,
    ):
        if not self.attachment_storage:
            raise AppError(
                "附件存储未配置",
                code="ATTACHMENT_STORAGE_UNAVAILABLE",
                status=503,
            )
        clean_name = (
            unquote(filename or "attachment.bin")
            .replace("\\", "/")
            .split("/")[-1]
            .strip()
        )
        if not clean_name or len(clean_name) > 255:
            raise AppError(
                "附件文件名无效", code="ATTACHMENT_FILENAME_INVALID"
            )
        attachment_id = _uid("attachment")
        self.db.commit()
        stored = self.attachment_storage.store(
            user_id=self.user_id,
            target_type=target_type,
            target_id=target_id,
            attachment_id=attachment_id,
            data=data,
        )
        attachment = ArtifactAttachment(
            id=attachment_id,
            learning_run_id=learning_run_id,
            user_id=self.user_id,
            target_type=target_type,
            target_id=target_id,
            original_filename=clean_name,
            media_type=(media_type or "application/octet-stream")[:160],
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            object_key=stored.object_key,
        )
        self.db.add(attachment)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.attachment_storage.resolve(stored.object_key).unlink(
                missing_ok=True
            )
            raise
        return self.attachment_view(attachment)

    def _validated_attachments(
        self,
        learning_run_id,
        target_type,
        target_id,
        attachment_ids,
    ):
        if not attachment_ids:
            raise AppError(
                "必须提交至少一个真实附件", code="ATTACHMENT_REQUIRED"
            )
        if len(set(attachment_ids)) != len(attachment_ids):
            raise AppError(
                "附件 ID 不得重复", code="ATTACHMENT_DUPLICATE"
            )
        attachments = self.db.scalars(
            select(ArtifactAttachment).where(
                ArtifactAttachment.id.in_(attachment_ids),
                ArtifactAttachment.user_id == self.user_id,
                ArtifactAttachment.learning_run_id == learning_run_id,
                ArtifactAttachment.target_type == target_type,
                ArtifactAttachment.target_id == target_id,
            )
        ).all()
        if len(attachments) != len(attachment_ids):
            raise AppError(
                "附件不存在、无权访问或不属于当前成果",
                code="ATTACHMENT_INVALID",
                status=403,
            )
        by_id = {item.id: item for item in attachments}
        return [by_id[item_id] for item_id in attachment_ids]

    def _attachments(self, learning_run_id, target_type, target_id):
        items = self.db.scalars(
            select(ArtifactAttachment)
            .where(
                ArtifactAttachment.user_id == self.user_id,
                ArtifactAttachment.learning_run_id == learning_run_id,
                ArtifactAttachment.target_type == target_type,
                ArtifactAttachment.target_id == target_id,
            )
            .order_by(ArtifactAttachment.created_at)
        ).all()
        return [self.attachment_view(item) for item in items]

    @staticmethod
    def attachment_view(item) -> dict:
        return {
            "id": item.id,
            "filename": item.original_filename,
            "mediaType": item.media_type,
            "byteSize": item.byte_size,
            "sha256": item.sha256,
            "createdAt": _timestamp(item.created_at),
        }
