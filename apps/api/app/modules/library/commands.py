import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Book,
    Chapter,
    ChapterProgress,
    ChapterRevision,
    LearningPlan,
    Section,
    Series,
    Shelf,
    now,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _timestamp(value):
    return value.isoformat() if value else None


class CatalogCommandService:
    """Own explicit user edits to shelves and future curriculum structure."""

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        contexts,
        progress,
        shelf_view: Callable,
        book_view: Callable[[str], dict],
        chapter_view: Callable,
    ):
        self.db = db
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress
        self.shelf_view = shelf_view
        self.book_view = book_view
        self.chapter_view = chapter_view

    def create_shelf(self, body) -> dict:
        row = Shelf(
            id=_uid("shelf"),
            user_id=self.user_id,
            name=body.name,
            domain="",
            specialty="",
            tags_json="[]",
            origin="user_created",
        )
        self.db.add(row)
        self.db.commit()
        return self.shelf_view(row)

    def rename_shelf(self, shelf_id: str, body) -> dict:
        row = self.db.scalar(
            select(Shelf).where(
                Shelf.id == shelf_id,
                Shelf.user_id == self.user_id,
                Shelf.deleted_at.is_(None),
            )
        )
        if not row:
            raise AppError("书架不存在", code="SHELF_NOT_FOUND", status=404)
        row.name = body.name
        self.db.commit()
        return self.shelf_view(row)

    def delete_shelf(self, shelf_id: str) -> None:
        shelf = self.db.scalar(
            select(Shelf).where(
                Shelf.id == shelf_id,
                Shelf.user_id == self.user_id,
                Shelf.deleted_at.is_(None),
            )
        )
        if not shelf:
            raise AppError("书架不存在", code="SHELF_NOT_FOUND", status=404)

        deleted_at = now()
        series_rows = self.db.scalars(
            select(Series).where(
                Series.shelf_id == shelf.id,
                Series.deleted_at.is_(None),
            )
        ).all()
        for series in series_rows:
            series.deleted_at = deleted_at
            plan = self.db.get(LearningPlan, series.plan_id)
            if plan:
                plan.status = "deleted"
        shelf.deleted_at = deleted_at
        self.db.commit()

    def delete_series(self, series_id: str) -> None:
        series = self.contexts.resolve_series(
            user_id=self.user_id,
            series_id=series_id,
        ).series
        series.deleted_at = now()
        plan = self.db.get(LearningPlan, series.plan_id)
        if plan:
            plan.status = "deleted"
        self.db.commit()

    def delete_book(self, book_id: str) -> None:
        book = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        ).book
        deleted_at = now()
        book.deleted_at = deleted_at
        self.db.add(
            ChapterRevision(
                id=_uid("revision"),
                book_id=book.id,
                action="book_soft_delete",
                before_json=_dump(
                    {
                        "id": book.id,
                        "seriesId": book.series_id,
                        "position": book.position,
                        "title": book.title,
                        "status": self.progress.for_book(book).status,
                    }
                ),
                after_json=_dump({"deletedAt": _timestamp(deleted_at)}),
            )
        )
        remaining = self.db.scalars(
            select(Book)
            .where(
                Book.series_id == book.series_id,
                Book.id != book.id,
                Book.deleted_at.is_(None),
            )
            .order_by(Book.position)
        ).all()
        if not remaining:
            series = self.db.get(Series, book.series_id)
            series.deleted_at = deleted_at
            plan = self.db.get(LearningPlan, series.plan_id)
            if plan:
                plan.status = "deleted"
        elif not any(
            self.progress.for_book(item).status != "locked" for item in remaining
        ):
            first_book = remaining[0]
            if first_book.outline_status != "confirmed":
                self.db.commit()
                return
            self.progress.set_status(
                self.progress.for_book(first_book), "available"
            )
            first_chapter = self.db.scalar(
                select(Chapter)
                .where(Chapter.book_id == first_book.id)
                .order_by(Chapter.position)
            )
            if first_chapter:
                chapter_progress = self.progress.for_chapter(
                    first_chapter, first_book
                )
                if chapter_progress.status == "locked":
                    self.progress.set_status(chapter_progress, "available")
            first_section = self.db.scalar(
                select(Section)
                .join(Chapter, Chapter.id == Section.chapter_id)
                .where(Chapter.book_id == first_book.id)
                .order_by(Chapter.position, Section.position)
            )
            if first_section and first_chapter:
                section_progress = self.progress.for_section(
                    first_section, first_chapter, first_book
                )
                if section_progress.status == "locked":
                    self.progress.set_status(section_progress, "available")
        self.db.commit()

    def add_chapter(self, book_id: str, body) -> dict:
        book = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        ).book
        chapters = self.db.scalars(
            select(Chapter)
            .where(Chapter.book_id == book.id)
            .order_by(Chapter.position)
        ).all()
        started_end = max(
            (
                item.position
                for item in chapters
                if self.progress.for_chapter(item, book).status == "completed"
                or self._has_sections(item.id)
            ),
            default=0,
        )
        position = body.position or len(chapters) + 1
        if position <= started_end or position > len(chapters) + 1:
            raise AppError(
                "只能在未开始章节范围内新增",
                code="CHAPTER_POSITION_INVALID",
                status=409,
            )
        for item in reversed(chapters):
            if item.position >= position:
                item.position += 1000
        self.db.flush()
        for item in chapters:
            if item.position >= 1000:
                item.position -= 999
        row = Chapter(
            id=_uid("chapter"),
            book_id=book.id,
            position=position,
            title=body.title,
            objective=body.objective,
        )
        self.db.add(row)
        self.progress.add_chapter(
            self.progress.active_run(book.series_id),
            row,
            status="locked",
        )
        self.db.add(
            ChapterRevision(
                id=_uid("revision"),
                book_id=book.id,
                action="add",
                after_json=_dump(
                    {"id": row.id, "position": position, "title": row.title}
                ),
            )
        )
        self.db.commit()
        return self.chapter_view(row)

    def update_chapter(self, chapter_id: str, body) -> dict:
        chapter = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        ).chapter
        self._assert_future(chapter)
        before = {"title": chapter.title, "objective": chapter.objective}
        if body.title is not None:
            chapter.title = body.title
        if body.objective is not None:
            chapter.objective = body.objective
        self.db.add(
            ChapterRevision(
                id=_uid("revision"),
                book_id=chapter.book_id,
                action="update",
                before_json=_dump(before),
                after_json=_dump(
                    {"title": chapter.title, "objective": chapter.objective}
                ),
            )
        )
        self.db.commit()
        return self.chapter_view(chapter)

    def delete_chapter(self, chapter_id: str) -> None:
        chapter = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        ).chapter
        self._assert_future(chapter)
        book_id = chapter.book_id
        old_position = chapter.position
        count = self.db.scalar(
            select(func.count())
            .select_from(Chapter)
            .where(Chapter.book_id == book_id)
        )
        if count <= 1:
            raise AppError("一本书至少保留一章", code="LAST_CHAPTER", status=409)
        self.db.add(
            ChapterRevision(
                id=_uid("revision"),
                book_id=book_id,
                action="delete",
                before_json=_dump(
                    {
                        "id": chapter.id,
                        "position": old_position,
                        "title": chapter.title,
                    }
                ),
            )
        )
        projection = self.db.scalar(
            select(ChapterProgress).where(
                ChapterProgress.user_id == self.user_id,
                ChapterProgress.chapter_id == chapter.id,
            )
        )
        if projection:
            self.db.delete(projection)
            self.db.flush()
        self.db.delete(chapter)
        self.db.flush()
        later = self.db.scalars(
            select(Chapter)
            .where(
                Chapter.book_id == book_id,
                Chapter.position > old_position,
            )
            .order_by(Chapter.position)
        ).all()
        for item in later:
            item.position += 1000
        self.db.flush()
        for item in later:
            item.position -= 1001
        self.db.commit()

    def reorder_chapters(self, book_id: str, chapter_ids: list[str]) -> dict:
        book = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        ).book
        chapters = self.db.scalars(
            select(Chapter)
            .where(Chapter.book_id == book_id)
            .order_by(Chapter.position)
        ).all()
        future = [
            item
            for item in chapters
            if not self._has_sections(item.id)
            and self.progress.for_chapter(item, book).status != "completed"
        ]
        if (
            set(chapter_ids) != {item.id for item in future}
            or len(chapter_ids) != len(future)
        ):
            raise AppError(
                "排序必须且只能包含全部未开始章节",
                code="CHAPTER_ORDER_INVALID",
                status=409,
            )
        slots = sorted(item.position for item in future)
        by_id = {item.id: item for item in future}
        before = [
            item.id for item in sorted(future, key=lambda value: value.position)
        ]
        for item in future:
            item.position += 1000
        self.db.flush()
        for position, chapter_id in zip(slots, chapter_ids, strict=True):
            by_id[chapter_id].position = position
        self.db.add(
            ChapterRevision(
                id=_uid("revision"),
                book_id=book_id,
                action="reorder",
                before_json=_dump(before),
                after_json=_dump(chapter_ids),
            )
        )
        self.db.commit()
        return self.book_view(book_id)

    def _assert_future(self, chapter: Chapter) -> None:
        if (
            self.progress.for_chapter(chapter).status == "completed"
            or self._has_sections(chapter.id)
        ):
            raise AppError(
                "已开始章节不能调整",
                code="CHAPTER_ALREADY_STARTED",
                status=409,
            )

    def _has_sections(self, chapter_id: str) -> bool:
        return bool(
            self.db.scalar(
                select(func.count())
                .select_from(Section)
                .where(Section.chapter_id == chapter_id)
            )
        )
