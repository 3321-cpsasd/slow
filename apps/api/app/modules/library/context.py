from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import Book, Chapter, Section, Series, Shelf


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
