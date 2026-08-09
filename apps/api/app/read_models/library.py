import json
from collections import defaultdict

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..modules.curriculum.policy import CHAPTER_SECTION_POLICY
from ..infrastructure.tables import (
    ArtifactAttachment,
    ArtifactProgress,
    Book,
    BookCapstone,
    BookProgress,
    Chapter,
    ChapterPractice,
    ChapterProgress,
    LearningRun,
    Section,
    SectionProgress,
    Series,
    Shelf,
    User,
)

EXPECTED_SECTIONS_PER_CHAPTER = 4


def _load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _timestamp(value):
    return value.isoformat() if value else None


class LibraryReadModel:
    """Builds library pages with a fixed number of SELECTs and no writes."""

    def __init__(self, db: Session, *, user_id: str):
        self.db = db
        self.user_id = user_id

    def bootstrap(self) -> dict:
        user = self.db.get(User, self.user_id)
        shelf_rows = self.db.execute(
            select(Shelf)
            .where(Shelf.user_id == self.user_id)
            .order_by(Shelf.name, Shelf.id)
        ).scalars().all()
        shelves = list(shelf_rows)
        rows = self.db.execute(
            select(Series, LearningRun)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .outerjoin(
                LearningRun,
                and_(
                    LearningRun.series_id == Series.id,
                    LearningRun.user_id == self.user_id,
                    LearningRun.status == "active",
                ),
            )
            .where(
                Shelf.user_id == self.user_id,
                Series.deleted_at.is_(None),
            )
            .order_by(Series.id)
        ).all()
        if any(run is None for _series, run in rows):
            raise AppError(
                "学习运行投影缺失",
                code="LEARNING_RUN_MISSING",
                status=500,
            )
        built = self._build(rows)
        by_shelf = defaultdict(list)
        for row in built:
            by_shelf[row.pop("_shelfId")].append(row)
        return {
            "user": {
                "id": self.user_id,
                "name": user.name if user else "",
            },
            "shelves": [
                {
                    "id": shelf.id,
                    "name": shelf.name,
                    "domain": shelf.domain,
                    "specialty": shelf.specialty,
                    "tags": _load(shelf.tags_json, []),
                    "series": by_shelf[shelf.id],
                }
                for shelf in shelves
            ]
        }

    def series(self, series_id: str) -> dict:
        rows = self.db.execute(
            select(Series, LearningRun)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .outerjoin(
                LearningRun,
                and_(
                    LearningRun.series_id == Series.id,
                    LearningRun.user_id == self.user_id,
                    LearningRun.status == "active",
                ),
            )
            .where(
                Series.id == series_id,
                Series.deleted_at.is_(None),
                Shelf.user_id == self.user_id,
            )
        ).all()
        if not rows:
            raise AppError("系列不存在", code="SERIES_NOT_FOUND", status=404)
        if rows[0][1] is None:
            raise AppError(
                "学习运行投影缺失",
                code="LEARNING_RUN_MISSING",
                status=500,
            )
        result = self._build(rows)[0]
        result.pop("_shelfId", None)
        return result

    def book(self, book_id: str) -> dict:
        row = self.db.execute(
            select(Book.series_id)
            .join(Series, Series.id == Book.series_id)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Book.id == book_id,
                Book.deleted_at.is_(None),
                Series.deleted_at.is_(None),
                Book.shelf_id == Series.shelf_id,
                Shelf.user_id == self.user_id,
            )
        ).one_or_none()
        if not row:
            raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)
        series = self.series(row.series_id)
        for book in series["books"]:
            if book["id"] == book_id:
                return book
        raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)

    def _build(self, series_rows) -> list[dict]:
        if not series_rows:
            return []
        series_ids = [series.id for series, _run in series_rows]
        run_ids = [run.id for _series, run in series_rows]

        book_rows = self.db.execute(
            select(Book, BookProgress)
            .join(
                LearningRun,
                and_(
                    LearningRun.series_id == Book.series_id,
                    LearningRun.id.in_(run_ids),
                ),
            )
            .outerjoin(
                BookProgress,
                and_(
                    BookProgress.learning_run_id == LearningRun.id,
                    BookProgress.book_id == Book.id,
                ),
            )
            .where(
                Book.series_id.in_(series_ids),
                Book.deleted_at.is_(None),
            )
            .order_by(Book.series_id, Book.position)
        ).all()
        self._require_projection(
            book_rows,
            "BOOK_PROGRESS_MISSING",
            "书籍",
        )
        book_ids = [book.id for book, _progress in book_rows]

        chapter_rows = self.db.execute(
            select(Chapter, ChapterProgress)
            .join(Book, Book.id == Chapter.book_id)
            .join(
                LearningRun,
                and_(
                    LearningRun.series_id == Book.series_id,
                    LearningRun.id.in_(run_ids),
                ),
            )
            .outerjoin(
                ChapterProgress,
                and_(
                    ChapterProgress.learning_run_id == LearningRun.id,
                    ChapterProgress.chapter_id == Chapter.id,
                ),
            )
            .where(Chapter.book_id.in_(book_ids))
            .order_by(Chapter.book_id, Chapter.position)
        ).all() if book_ids else []
        self._require_projection(
            chapter_rows,
            "CHAPTER_PROGRESS_MISSING",
            "章节",
        )
        chapter_ids = [chapter.id for chapter, _progress in chapter_rows]

        section_rows = self.db.execute(
            select(Section, SectionProgress)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(
                LearningRun,
                and_(
                    LearningRun.series_id == Book.series_id,
                    LearningRun.id.in_(run_ids),
                ),
            )
            .outerjoin(
                SectionProgress,
                and_(
                    SectionProgress.learning_run_id == LearningRun.id,
                    SectionProgress.section_id == Section.id,
                ),
            )
            .where(Section.chapter_id.in_(chapter_ids))
            .order_by(Section.chapter_id, Section.position)
        ).all() if chapter_ids else []
        self._require_projection(
            section_rows,
            "SECTION_PROGRESS_MISSING",
            "小节",
        )

        practice_rows = self.db.execute(
            select(ChapterPractice, ArtifactProgress)
            .join(Chapter, Chapter.id == ChapterPractice.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .join(
                LearningRun,
                and_(
                    LearningRun.series_id == Book.series_id,
                    LearningRun.id.in_(run_ids),
                ),
            )
            .outerjoin(
                ArtifactProgress,
                and_(
                    ArtifactProgress.learning_run_id == LearningRun.id,
                    ArtifactProgress.target_type == "chapter_practice",
                    ArtifactProgress.target_id == ChapterPractice.id,
                ),
            )
            .where(ChapterPractice.chapter_id.in_(chapter_ids))
        ).all() if chapter_ids else []
        self._require_projection(
            practice_rows,
            "ARTIFACT_PROGRESS_MISSING",
            "章末实践",
        )

        capstone_rows = self.db.execute(
            select(BookCapstone, ArtifactProgress)
            .join(Book, Book.id == BookCapstone.book_id)
            .join(
                LearningRun,
                and_(
                    LearningRun.series_id == Book.series_id,
                    LearningRun.id.in_(run_ids),
                ),
            )
            .outerjoin(
                ArtifactProgress,
                and_(
                    ArtifactProgress.learning_run_id == LearningRun.id,
                    ArtifactProgress.target_type == "book_capstone",
                    ArtifactProgress.target_id == BookCapstone.id,
                ),
            )
            .where(BookCapstone.book_id.in_(book_ids))
        ).all() if book_ids else []
        self._require_projection(
            capstone_rows,
            "ARTIFACT_PROGRESS_MISSING",
            "全书任务",
        )

        attachments = list(
            self.db.scalars(
                select(ArtifactAttachment)
                .where(
                    ArtifactAttachment.user_id == self.user_id,
                    ArtifactAttachment.learning_run_id.in_(run_ids),
                )
                .order_by(ArtifactAttachment.created_at)
            ).all()
        )
        attachments_by_target = defaultdict(list)
        for item in attachments:
            attachments_by_target[
                (item.learning_run_id, item.target_type, item.target_id)
            ].append(
                {
                    "id": item.id,
                    "filename": item.original_filename,
                    "mediaType": item.media_type,
                    "byteSize": item.byte_size,
                    "sha256": item.sha256,
                    "createdAt": _timestamp(item.created_at),
                }
            )

        run_by_series = {series.id: run for series, run in series_rows}
        sections_by_chapter = defaultdict(list)
        for section, progress in section_rows:
            sections_by_chapter[section.chapter_id].append(
                {
                    "id": section.id,
                    "position": section.position,
                    "title": section.title,
                    "question": section.question,
                    "objectives": _load(section.objectives_json, []),
                    "status": progress.status,
                    "bestScore": progress.best_score,
                    "totalScore": progress.total_score,
                    "askMeUnlocked": progress.ask_me_unlocked,
                }
            )

        practice_by_chapter = {}
        for practice, progress in practice_rows:
            files = attachments_by_target[
                (
                    progress.learning_run_id,
                    "chapter_practice",
                    practice.id,
                )
            ]
            practice_by_chapter[practice.chapter_id] = {
                "id": practice.id,
                "title": practice.title,
                "instructions": _load(practice.instructions_json, {}),
                "submission": _load(progress.submission_json, {}),
                "attachments": files,
                "evidenceMode": (
                    "file_attachment" if files else "structured_only_legacy"
                ),
                "status": progress.status,
            }

        chapters_by_book = defaultdict(list)
        section_progress_by_book = defaultdict(list)
        practice_status_by_book = defaultdict(list)
        for chapter, progress in chapter_rows:
            sections = sections_by_chapter[chapter.id]
            practice = practice_by_chapter.get(chapter.id)
            chapters_by_book[chapter.book_id].append(
                {
                    "id": chapter.id,
                    "position": chapter.position,
                    "title": chapter.title,
                    "objective": chapter.objective,
                    "status": progress.status,
                    "generated": bool(sections),
                    "workloadHint": (
                        CHAPTER_SECTION_POLICY.workload(len(sections))
                        if sections
                        else None
                    ),
                    "sections": sections,
                    "practice": practice,
                }
            )
            section_progress_by_book[chapter.book_id].append(sections)
            practice_status_by_book[chapter.book_id].append(
                practice["status"] if practice else "locked"
            )

        capstone_by_book = {}
        for capstone, progress in capstone_rows:
            files = attachments_by_target[
                (progress.learning_run_id, "book_capstone", capstone.id)
            ]
            capstone_by_book[capstone.book_id] = {
                "id": capstone.id,
                "title": capstone.title,
                "brief": _load(capstone.brief_json, {}),
                "submission": _load(progress.submission_json, {}),
                "attachments": files,
                "evidenceMode": (
                    "file_attachment" if files else "structured_only_legacy"
                ),
                "status": progress.status,
            }

        books_by_series = defaultdict(list)
        book_ratio = {}
        for book, progress in book_rows:
            chapters = chapters_by_book[book.id]
            section_units = 0.0
            for sections in section_progress_by_book[book.id]:
                denominator = len(sections) or EXPECTED_SECTIONS_PER_CHAPTER
                completed = sum(
                    item["status"] == "completed" for item in sections
                )
                section_units += completed / denominator
            ratio = section_units / len(chapters) if chapters else 0
            book_ratio[book.id] = ratio
            capstone = capstone_by_book.get(book.id)
            artifact_statuses = practice_status_by_book[book.id] + [
                capstone["status"] if capstone else "locked"
            ]
            practice_ratio = (
                sum(status == "completed" for status in artifact_statuses)
                / len(artifact_statuses)
                if artifact_statuses
                else 0
            )
            books_by_series[book.series_id].append(
                {
                    "id": book.id,
                    "seriesId": book.series_id,
                    "position": book.position,
                    "title": book.title,
                    "description": book.description,
                    "estimatedMinutes": book.estimated_minutes,
                    "outlineStatus": book.outline_status,
                    "outlineVersion": book.outline_version,
                    "outlineConfirmedAt": _timestamp(book.outline_confirmed_at),
                    "status": progress.status,
                    "progress": round(ratio * 100),
                    "practiceProgress": round(practice_ratio * 100),
                    "chapters": chapters,
                    "capstone": capstone,
                }
            )

        results = []
        for series, _run in series_rows:
            books = books_by_series[series.id]
            total_minutes = sum(book["estimatedMinutes"] for book in books) or 1
            completed_minutes = sum(
                book["estimatedMinutes"] * book_ratio[book["id"]]
                for book in books
            )
            results.append(
                {
                    "_shelfId": series.shelf_id,
                    "id": series.id,
                    "title": series.title,
                    "rationale": series.rationale,
                    "progress": round(completed_minutes / total_minutes * 100),
                    "progressBasis": "bookEstimatedMinutesWithFutureChapterProjection",
                    "books": books,
                }
            )
        return results

    @staticmethod
    def _require_projection(rows, code: str, label: str) -> None:
        if any(progress is None for _entity, progress in rows):
            raise AppError(
                f"{label}进度投影缺失",
                code=code,
                status=500,
            )
