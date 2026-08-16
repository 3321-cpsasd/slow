import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ContentVersion,
    LearningRunSectionBinding,
    ReadingAnnotation,
    ReadingAnnotationRevision,
    now,
)
from ..learning.progress import ProgressStore


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _block_hash(block: dict) -> str:
    visible_identity = {
        key: block.get(key)
        for key in (
            "blockKey",
            "kind",
            "role",
            "heading",
            "content",
        )
    }
    return hashlib.sha256(
        json.dumps(
            visible_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class ReadingAnnotationService:
    """Sole writer and reader for version-bound user reading marks."""

    def __init__(self, db: Session, *, user_id: str, contexts, progress: ProgressStore):
        self.db = db
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress

    def list(self, section_id: str) -> dict:
        learning_run, binding = self._binding(section_id)
        current_content = self.db.get(ContentVersion, binding.content_version_id)
        current_blocks = _load(current_content.blocks_json, []) if current_content else []
        annotations = self.db.scalars(
            select(ReadingAnnotation)
            .where(
                ReadingAnnotation.learning_run_id == learning_run.id,
                ReadingAnnotation.user_id == self.user_id,
                ReadingAnnotation.section_id == section_id,
                ReadingAnnotation.status == "active",
            )
            .order_by(ReadingAnnotation.created_at, ReadingAnnotation.id)
        ).all()
        source_content_ids = {item.content_version_id for item in annotations}
        versions = {
            item.id: item
            for item in self.db.scalars(
                select(ContentVersion).where(ContentVersion.id.in_(source_content_ids))
            ).all()
        } if source_content_ids else {}
        source_blocks_by_content = {
            content_id: {
                str(block.get("id")): block
                for block in _load(content.blocks_json, [])
                if isinstance(block, dict)
            }
            for content_id, content in versions.items()
        }
        return {
            "sectionId": section_id,
            "currentContentVersionId": binding.content_version_id,
            "items": [
                self._view(
                    item,
                    source_block=source_blocks_by_content
                    .get(item.content_version_id, {})
                    .get(item.block_id),
                    source_version=versions.get(item.content_version_id),
                    current_content=current_content,
                    current_blocks=current_blocks,
                )
                for item in annotations
            ],
        }

    def create(self, section_id: str, body, idempotency_key: str) -> dict:
        request_key = idempotency_key.strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "标注请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        learning_run, binding = self._binding(section_id)
        content, block = self._bound_block(
            binding,
            section_id=section_id,
            content_version_id=body.content_version_id,
            block_id=body.block_id,
        )
        request_hash = hashlib.sha256(
            json.dumps(
                body.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        replay = self.db.scalar(
            select(ReadingAnnotation).where(
                ReadingAnnotation.user_id == self.user_id,
                ReadingAnnotation.learning_run_id == learning_run.id,
                ReadingAnnotation.idempotency_key == request_key,
            )
        )
        if replay:
            if replay.request_hash != request_hash:
                raise AppError(
                    "标注请求标识已经用于其他内容",
                    code="ANNOTATION_IDEMPOTENCY_CONFLICT",
                    status=409,
                )
            return self._view(
                replay,
                source_block=block,
                source_version=content,
                current_content=self.db.get(ContentVersion, binding.content_version_id),
                current_blocks=_load(
                    self.db.get(ContentVersion, binding.content_version_id).blocks_json,
                    [],
                ),
            )
        annotation = ReadingAnnotation(
            id=_uid("annotation"),
            learning_run_id=learning_run.id,
            user_id=self.user_id,
            section_id=section_id,
            content_version_id=content.id,
            block_id=body.block_id,
            kind=body.kind,
            quote_exact=body.anchor.exact,
            quote_prefix=body.anchor.prefix,
            quote_suffix=body.anchor.suffix,
            start_offset=body.anchor.start_offset,
            end_offset=body.anchor.end_offset,
            block_snapshot_hash=_block_hash(block),
            body=body.body,
            color=body.color,
            status="active",
            version=1,
            idempotency_key=request_key,
            request_hash=request_hash,
        )
        self.db.add(annotation)
        self.db.add(ReadingAnnotationRevision(
            id=_uid("annotation_revision"),
            annotation_id=annotation.id,
            version=1,
            body=annotation.body,
            color=annotation.color,
            status="active",
            source="user_create",
        ))
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self.db.scalar(
                select(ReadingAnnotation).where(
                    ReadingAnnotation.user_id == self.user_id,
                    ReadingAnnotation.learning_run_id == learning_run.id,
                    ReadingAnnotation.idempotency_key == request_key,
                )
            )
            if not replay or replay.request_hash != request_hash:
                raise
            annotation = replay
        return self.get(annotation.id)

    def get(self, annotation_id: str) -> dict:
        annotation = self._owned(annotation_id)
        listing = self.list(annotation.section_id)
        return next(
            item for item in listing["items"] if item["id"] == annotation.id
        )

    def update(self, annotation_id: str, body) -> dict:
        annotation = self._owned(annotation_id)
        if annotation.status != "active":
            raise AppError("标注已经删除", code="ANNOTATION_DELETED", status=409)
        if body.body is not None:
            normalized = body.body.strip()
            if annotation.kind != "comment":
                raise AppError(
                    "高亮没有可编辑的批注正文",
                    code="ANNOTATION_KIND_MISMATCH",
                    status=409,
                )
            if not normalized:
                raise AppError("批注内容不能为空", code="ANNOTATION_BODY_REQUIRED", status=400)
            annotation.body = normalized
        if body.color is not None:
            annotation.color = body.color
        annotation.version += 1
        annotation.updated_at = now()
        self.db.add(ReadingAnnotationRevision(
            id=_uid("annotation_revision"),
            annotation_id=annotation.id,
            version=annotation.version,
            body=annotation.body,
            color=annotation.color,
            status=annotation.status,
            source="user_edit",
        ))
        self.db.commit()
        return self.get(annotation.id)

    def delete(self, annotation_id: str) -> None:
        annotation = self._owned(annotation_id)
        if annotation.status == "deleted":
            return
        annotation.status = "deleted"
        annotation.version += 1
        annotation.updated_at = now()
        self.db.add(ReadingAnnotationRevision(
            id=_uid("annotation_revision"),
            annotation_id=annotation.id,
            version=annotation.version,
            body=annotation.body,
            color=annotation.color,
            status="deleted",
            source="user_delete",
        ))
        self.db.commit()

    def _binding(self, section_id: str):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )
        if not binding:
            raise AppError(
                "请先打开本节再添加标注",
                code="ANNOTATION_SECTION_NOT_OPEN",
                status=409,
            )
        return learning_run, binding

    def _bound_block(
        self,
        binding,
        *,
        section_id: str,
        content_version_id: str,
        block_id: str,
    ):
        allowed_content_ids = {binding.content_version_id}
        audit = _load(binding.lineage_audit_json, {})
        for item in [
            *(audit.get("regenerations") or []),
            *(audit.get("feedbackRepairs") or []),
        ]:
            allowed_content_ids.update(filter(None, (
                item.get("fromContentVersionId"),
                item.get("toContentVersionId"),
            )))
        if content_version_id not in allowed_content_ids:
            raise AppError(
                "这份正文不属于当前学习记录",
                code="ANNOTATION_CONTENT_VERSION_INVALID",
                status=409,
            )
        content = self.db.get(ContentVersion, content_version_id)
        if not content or content.section_id != section_id:
            raise AppError(
                "标注对应的正文版本不存在",
                code="ANNOTATION_CONTENT_NOT_FOUND",
                status=404,
            )
        block = next(
            (
                item for item in _load(content.blocks_json, [])
                if isinstance(item, dict) and item.get("id") == block_id
            ),
            None,
        )
        if not block:
            raise AppError(
                "标注对应的正文段落不存在",
                code="ANNOTATION_BLOCK_NOT_FOUND",
                status=404,
            )
        return content, block

    def _owned(self, annotation_id: str) -> ReadingAnnotation:
        annotation = self.db.scalar(
            select(ReadingAnnotation).where(
                ReadingAnnotation.id == annotation_id,
                ReadingAnnotation.user_id == self.user_id,
            )
        )
        if not annotation:
            raise AppError("标注不存在", code="ANNOTATION_NOT_FOUND", status=404)
        return annotation

    def _view(
        self,
        annotation,
        *,
        source_block,
        source_version,
        current_content,
        current_blocks,
    ) -> dict:
        display_block_id = None
        anchor_status = "old_version"
        if annotation.content_version_id == current_content.id:
            display_block_id = annotation.block_id
            anchor_status = "current"
        elif source_block:
            exact_matches = [
                block for block in current_blocks
                if isinstance(block, dict)
                and _block_hash(block) == annotation.block_snapshot_hash
                and (
                    not source_block.get("blockKey")
                    or block.get("blockKey") == source_block.get("blockKey")
                )
            ]
            if len(exact_matches) == 1:
                display_block_id = exact_matches[0].get("id")
                anchor_status = "unchanged_in_current"
        return {
            "id": annotation.id,
            "sectionId": annotation.section_id,
            "contentVersionId": annotation.content_version_id,
            "contentVersion": source_version.version if source_version else None,
            "blockId": annotation.block_id,
            "displayBlockId": display_block_id,
            "anchorStatus": anchor_status,
            "kind": annotation.kind,
            "anchor": {
                "exact": annotation.quote_exact,
                "prefix": annotation.quote_prefix,
                "suffix": annotation.quote_suffix,
                "startOffset": annotation.start_offset,
                "endOffset": annotation.end_offset,
            },
            "body": annotation.body,
            "color": annotation.color,
            "version": annotation.version,
            "createdAt": annotation.created_at.isoformat(),
            "updatedAt": annotation.updated_at.isoformat(),
        }
