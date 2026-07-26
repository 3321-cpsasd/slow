from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Book,
    BookProgress,
    Chapter,
    ChapterProgress,
    LearningRun,
    Section,
    SectionProgress,
    now,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ProgressStore:
    """The only write gateway for user learning-state projections."""

    def __init__(self, db: Session, *, user_id: str):
        self.db = db
        self.user_id = user_id

    def create_run(self, series_id: str) -> LearningRun:
        run = LearningRun(
            id=_uid("learning_run"),
            user_id=self.user_id,
            series_id=series_id,
            status="active",
        )
        self.db.add(run)
        return run

    def active_run(self, series_id: str) -> LearningRun:
        run = self.db.scalar(
            select(LearningRun)
            .where(
                LearningRun.user_id == self.user_id,
                LearningRun.series_id == series_id,
                LearningRun.status == "active",
            )
            .order_by(LearningRun.created_at.desc())
        )
        if run:
            return run
        raise AppError(
            "学习运行投影缺失",
            code="LEARNING_RUN_MISSING",
            status=500,
        )

    def add_book(
        self,
        run: LearningRun,
        book: Book,
        *,
        status: str,
    ) -> BookProgress:
        row = BookProgress(
            id=_uid("book_progress"),
            learning_run_id=run.id,
            user_id=self.user_id,
            book_id=book.id,
            status=status,
        )
        self.db.add(row)
        return row

    def add_chapter(
        self,
        run: LearningRun,
        chapter: Chapter,
        *,
        status: str,
    ) -> ChapterProgress:
        row = ChapterProgress(
            id=_uid("chapter_progress"),
            learning_run_id=run.id,
            user_id=self.user_id,
            chapter_id=chapter.id,
            status=status,
        )
        self.db.add(row)
        return row

    def add_section(
        self,
        run: LearningRun,
        section: Section,
        *,
        status: str,
    ) -> SectionProgress:
        row = SectionProgress(
            id=_uid("section_progress"),
            learning_run_id=run.id,
            user_id=self.user_id,
            section_id=section.id,
            status=status,
        )
        self.db.add(row)
        return row

    def for_book(self, book: Book) -> BookProgress:
        run = self.active_run(book.series_id)
        row = self.db.scalar(
            select(BookProgress).where(
                BookProgress.learning_run_id == run.id,
                BookProgress.book_id == book.id,
            )
        )
        if not row:
            raise AppError(
                "书籍进度投影缺失",
                code="BOOK_PROGRESS_MISSING",
                status=500,
            )
        return row

    def for_chapter(self, chapter: Chapter, book: Book | None = None) -> ChapterProgress:
        book = book or self.db.get(Book, chapter.book_id)
        run = self.active_run(book.series_id)
        row = self.db.scalar(
            select(ChapterProgress).where(
                ChapterProgress.learning_run_id == run.id,
                ChapterProgress.chapter_id == chapter.id,
            )
        )
        if not row:
            raise AppError(
                "章节进度投影缺失",
                code="CHAPTER_PROGRESS_MISSING",
                status=500,
            )
        return row

    def for_section(
        self,
        section: Section,
        chapter: Chapter | None = None,
        book: Book | None = None,
    ) -> SectionProgress:
        chapter = chapter or self.db.get(Chapter, section.chapter_id)
        book = book or self.db.get(Book, chapter.book_id)
        run = self.active_run(book.series_id)
        row = self.db.scalar(
            select(SectionProgress).where(
                SectionProgress.learning_run_id == run.id,
                SectionProgress.section_id == section.id,
            )
        )
        if not row:
            raise AppError(
                "小节进度投影缺失",
                code="SECTION_PROGRESS_MISSING",
                status=500,
            )
        return row

    @staticmethod
    def set_status(progress, status: str) -> None:
        progress.status = status
        progress.updated_at = now()
