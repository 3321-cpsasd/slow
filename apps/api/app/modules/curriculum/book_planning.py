import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ...ai.port import AiPort
from ...core.errors import AppError
from ...infrastructure.tables import (
    Book,
    Chapter,
    ChapterProgress,
    ChapterRevision,
    Section,
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


class BookPlanningService:
    """Creates and confirms versioned proposals for a book's future chapters."""

    def __init__(
        self,
        db: Session,
        ai: AiPort,
        *,
        user_id: str,
        contexts,
        progress,
        missions,
        milestones,
        generation_contexts,
        memory_provider: Callable[[str], list[dict]],
        book_view: Callable[[str], dict],
    ):
        self.db = db
        self.ai = ai
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress
        self.missions = missions
        self.milestones = milestones
        self.generation_contexts = generation_contexts
        self.memory_provider = memory_provider
        self.book_view = book_view

    def _partition(self, book) -> tuple[list[Chapter], list[Chapter]]:
        chapters = self.db.scalars(
            select(Chapter)
            .where(Chapter.book_id == book.id)
            .order_by(Chapter.position)
        ).all()
        started: list[Chapter] = []
        future: list[Chapter] = []
        for chapter in chapters:
            generated = self.db.scalar(
                select(func.count())
                .select_from(Section)
                .where(Section.chapter_id == chapter.id)
            )
            target = (
                started
                if self.progress.for_chapter(chapter, book).status == "completed"
                or generated
                else future
            )
            target.append(chapter)
        return started, future

    @staticmethod
    def _snapshot(chapters: list[Chapter]) -> list[dict]:
        return [
            {
                "id": item.id,
                "title": item.title,
                "objective": item.objective,
                "position": item.position,
            }
            for item in chapters
        ]

    async def propose(self, book_id: str) -> dict:
        context = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        )
        book = context.book
        if book.outline_status == "draft" and not self._previous_book_completed(book):
            raise AppError(
                "请先完成上一本书，再按最新学习证据校准本书章节",
                code="PREVIOUS_BOOK_NOT_COMPLETED",
                status=409,
            )
        started, future = self._partition(book)
        request = {
            "title": book.title,
            "topic": book.topic,
            "description": book.description,
            "started_chapters": [
                {"title": item.title, "objective": item.objective}
                for item in started
            ],
            "future_chapters": [
                {"title": item.title, "objective": item.objective}
                for item in future
            ],
        }
        memory = self.memory_provider(book.shelf_id)
        mission = self.missions.current_version(context.series.id)
        context_pack = self.generation_contexts.build(
            "book_replan",
            shelf=context.shelf,
            series=context.series,
            book=book,
            mission=mission,
            memory=memory,
        )
        request = self.generation_contexts.attach(request, context_pack)
        self.db.commit()
        generated = await self.ai.replan_book(request, memory)
        proposal = ChapterRevision(
            id=_uid("revision"),
            book_id=book.id,
            action="ai_replan_proposal",
            before_json=_dump(self._snapshot(future)),
            after_json=_dump(
                {
                    "rationale": generated.rationale,
                    "chapters": [item.model_dump() for item in generated.chapters],
                }
            ),
        )
        self.db.add(proposal)
        self.db.commit()
        return {
            "proposalId": proposal.id,
            "rationale": generated.rationale,
            "chapters": [item.model_dump() for item in generated.chapters],
            "requiresConfirmation": True,
        }

    def confirm(self, book_id: str, proposal_id: str) -> dict:
        book = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        ).book
        proposal = self.db.get(ChapterRevision, proposal_id)
        if (
            not proposal
            or proposal.book_id != book_id
            or proposal.action != "ai_replan_proposal"
        ):
            raise AppError(
                "重规划提案不存在",
                code="REPLAN_PROPOSAL_NOT_FOUND",
                status=404,
            )
        started, future = self._partition(book)
        if self._snapshot(future) != _load(proposal.before_json, []):
            raise AppError(
                "未来章节已变化，请重新生成提案",
                code="REPLAN_PROPOSAL_STALE",
                status=409,
            )

        proposed = _load(proposal.after_json, {})
        future_ids = [item.id for item in future]
        if future_ids:
            self.db.execute(
                delete(ChapterProgress).where(
                    ChapterProgress.user_id == self.user_id,
                    ChapterProgress.chapter_id.in_(future_ids),
                )
            )
        for item in future:
            self.db.delete(item)
        self.db.flush()

        run = self.progress.active_run(book.series_id)
        old_by_position = {item.position: item.id for item in future}
        new_by_position = {}
        objective_by_chapter_id = {}
        for position, item in enumerate(proposed["chapters"], len(started) + 1):
            chapter = Chapter(
                id=_uid("chapter"),
                book_id=book.id,
                position=position,
                title=item["title"],
                objective=item["objective"],
            )
            self.db.add(chapter)
            self.progress.add_chapter(run, chapter, status="locked")
            new_by_position[position] = chapter.id
            objective_by_chapter_id[chapter.id] = chapter.objective
        self.db.flush()
        self.milestones.rebind_book_chapters(
            series_id=book.series_id,
            book_id=book.id,
            chapter_id_map={
                old_id: new_by_position[position]
                for position, old_id in old_by_position.items()
                if position in new_by_position
            },
            replaced_chapter_ids=set(old_by_position.values()),
            objective_by_chapter_id=objective_by_chapter_id,
        )
        book.outline_status = "confirmed"
        book.outline_version += 1
        book.outline_confirmed_at = now()
        proposal.action = "ai_replan_confirmed"
        self._unlock_after_previous_book(book)
        self.db.commit()
        return self.book_view(book.id)

    def _unlock_after_previous_book(self, book: Book) -> None:
        if book.position <= 1:
            return
        if not self._previous_book_completed(book):
            return
        book_progress = self.progress.for_book(book)
        if book_progress.status == "locked":
            self.progress.set_status(book_progress, "available")
        first_chapter = self.db.scalar(
            select(Chapter)
            .where(Chapter.book_id == book.id)
            .order_by(Chapter.position)
        )
        if first_chapter:
            chapter_progress = self.progress.for_chapter(first_chapter, book)
            if chapter_progress.status == "locked":
                self.progress.set_status(chapter_progress, "available")

    def _previous_book_completed(self, book: Book) -> bool:
        previous = self.db.scalar(
            select(Book)
            .where(
                Book.series_id == book.series_id,
                Book.position < book.position,
                Book.deleted_at.is_(None),
            )
            .order_by(Book.position.desc())
        )
        return not previous or self.progress.for_book(previous).status == "completed"
