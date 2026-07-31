from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Book,
    Chapter,
    LearningRun,
    LearningTask,
    Section,
    Series,
    Shelf,
)


@dataclass(frozen=True)
class SeriesContext:
    shelf: Shelf
    series: Series


@dataclass(frozen=True)
class BookContext(SeriesContext):
    book: Book


@dataclass(frozen=True)
class ChapterContext(BookContext):
    chapter: Chapter


@dataclass(frozen=True)
class SectionContext(ChapterContext):
    section: Section


@dataclass(frozen=True)
class LearningTaskContext(SectionContext):
    learning_run: LearningRun
    task: LearningTask


@dataclass(frozen=True)
class ChapterLearningTaskContext(ChapterContext):
    learning_run: LearningRun
    task: LearningTask


class ActiveLearningContextResolver:
    """The single authorization and active-ancestor boundary for learning resources."""

    def __init__(self, db: Session):
        self.db = db

    def resolve_series(self, *, user_id: str, series_id: str) -> SeriesContext:
        row = self.db.execute(
            select(Series, Shelf)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Series.id == series_id,
                Series.deleted_at.is_(None),
                Shelf.user_id == user_id,
            )
        ).one_or_none()
        if not row:
            raise AppError("系列不存在", code="SERIES_NOT_FOUND", status=404)
        series, shelf = row
        return SeriesContext(shelf=shelf, series=series)

    def resolve_book(self, *, user_id: str, book_id: str) -> BookContext:
        row = self.db.execute(
            select(Book, Series, Shelf)
            .join(Series, Series.id == Book.series_id)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Book.id == book_id,
                Book.shelf_id == Series.shelf_id,
                Book.deleted_at.is_(None),
                Series.deleted_at.is_(None),
                Shelf.user_id == user_id,
            )
        ).one_or_none()
        if not row:
            raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)
        book, series, shelf = row
        return BookContext(shelf=shelf, series=series, book=book)

    def resolve_chapter(self, *, user_id: str, chapter_id: str) -> ChapterContext:
        row = self.db.execute(
            select(Chapter, Book, Series, Shelf)
            .join(Book, Book.id == Chapter.book_id)
            .join(Series, Series.id == Book.series_id)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Chapter.id == chapter_id,
                Book.shelf_id == Series.shelf_id,
                Book.deleted_at.is_(None),
                Series.deleted_at.is_(None),
                Shelf.user_id == user_id,
            )
        ).one_or_none()
        if not row:
            raise AppError("章不存在", code="CHAPTER_NOT_FOUND", status=404)
        chapter, book, series, shelf = row
        return ChapterContext(shelf=shelf, series=series, book=book, chapter=chapter)

    def resolve_section(self, *, user_id: str, section_id: str) -> SectionContext:
        row = self.db.execute(
            select(Section, Chapter, Book, Series, Shelf)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(Series, Series.id == Book.series_id)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Section.id == section_id,
                Book.shelf_id == Series.shelf_id,
                Book.deleted_at.is_(None),
                Series.deleted_at.is_(None),
                Shelf.user_id == user_id,
            )
        ).one_or_none()
        if not row:
            raise AppError("小节不存在", code="SECTION_NOT_FOUND", status=404)
        section, chapter, book, series, shelf = row
        return SectionContext(
            shelf=shelf,
            series=series,
            book=book,
            chapter=chapter,
            section=section,
        )

    def resolve_learning_task(
        self,
        *,
        user_id: str,
        task_id: str,
    ) -> LearningTaskContext:
        row = self.db.execute(
            select(
                LearningTask,
                LearningRun,
                Section,
                Chapter,
                Book,
                Series,
                Shelf,
            )
            .join(
                LearningRun,
                LearningRun.id == LearningTask.learning_run_id,
            )
            .join(Section, Section.id == LearningTask.section_id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(Series, Series.id == Book.series_id)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                LearningTask.id == task_id,
                LearningTask.user_id == user_id,
                LearningRun.user_id == user_id,
                LearningRun.series_id == Series.id,
                Book.shelf_id == Series.shelf_id,
                Shelf.user_id == user_id,
                Book.deleted_at.is_(None),
                Series.deleted_at.is_(None),
            )
        ).one_or_none()
        if not row:
            raise AppError(
                "任务、学习运行与小节不属于同一授权聚合",
                code="TASK_AGGREGATE_MISMATCH",
                status=409,
            )
        task, run, section, chapter, book, series, shelf = row
        return LearningTaskContext(
            shelf=shelf,
            series=series,
            book=book,
            chapter=chapter,
            section=section,
            learning_run=run,
            task=task,
        )

    def resolve_chapter_learning_task(
        self,
        *,
        user_id: str,
        task_id: str,
        chapter_id: str | None,
    ) -> ChapterLearningTaskContext:
        row = self.db.execute(
            select(
                LearningTask,
                LearningRun,
                Chapter,
                Book,
                Series,
                Shelf,
            )
            .join(
                LearningRun,
                LearningRun.id == LearningTask.learning_run_id,
            )
            .join(Chapter, Chapter.id == chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(Series, Series.id == Book.series_id)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                LearningTask.id == task_id,
                LearningTask.task_type == "initial_book_preload",
                LearningTask.section_id.is_(None),
                LearningTask.user_id == user_id,
                LearningRun.user_id == user_id,
                LearningRun.series_id == Series.id,
                Book.shelf_id == Series.shelf_id,
                Shelf.user_id == user_id,
                Book.deleted_at.is_(None),
                Series.deleted_at.is_(None),
            )
        ).one_or_none()
        if not row:
            raise AppError(
                "任务、学习运行与章节不属于同一授权聚合",
                code="TASK_AGGREGATE_MISMATCH",
                status=409,
            )
        task, run, chapter, book, series, shelf = row
        return ChapterLearningTaskContext(
            shelf=shelf,
            series=series,
            book=book,
            chapter=chapter,
            learning_run=run,
            task=task,
        )
