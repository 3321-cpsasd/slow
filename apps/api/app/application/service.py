from asyncio import CancelledError
import hashlib
import json
import re
from urllib.parse import unquote
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..auth.context import UserScope, WorkerExecutionContext

from ..ai.port import AiPort
from ..core.errors import AiError, AppError, safe_error_code
from ..demo_personas import LOCAL_DEMO_PERSONAS_BY_USER_ID
from ..infrastructure.tables import (
    ArtifactAttachment,
    ArtifactSubmission,
    AskMeSession,
    Book,
    BookCapstone,
    Chapter,
    ChapterProgress,
    ChapterPractice,
    ChapterRevision,
    ContentVersion,
    GenerationRun,
    LearningEvidence,
    LearningMemory,
    LearningNote,
    LearningRun,
    LearningTask,
    LearningPlan,
    LearningResumePosition,
    PlanCreationRequest,
    QaMessage,
    QaSession,
    QaThread,
    QuizAttempt,
    QuizSet,
    Remediation,
    Section,
    Series,
    Shelf,
    SourceVerification,
    User,
    now,
)
from ..modules.library.context import ActiveLearningContextResolver
from ..modules.artifacts.progress import ArtifactProgressStore
from ..modules.learning.commands import SubmitQuiz
from ..modules.learning.generation_leases import (
    acquire_generation_lease,
    release_generation_lease,
    renew_generation_lease,
)
from ..modules.learning.progress import ProgressStore
from ..modules.learning.tasks import (
    complete_task,
    fail_task,
    release_task,
    reset_failed_task,
    task_view,
)
from ..modules.tutoring.commands import GenerateLearningNote
from ..read_models.library import LibraryReadModel

DEMO_USER_ID = "user_demo"
EXPECTED_SECTIONS_PER_CHAPTER = 4


def uid(prefix):
    return f"{prefix}_{uuid4().hex}"


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def timestamp(value):
    return value.isoformat() if value else None


def normalized(value: str):
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


class SlowService:
    def __init__(
        self,
        db: Session,
        ai: AiPort,
        source_verifier,
        attachment_storage=None,
        *,
        scope: UserScope | WorkerExecutionContext,
    ):
        self.db, self.ai, self.source_verifier = db, ai, source_verifier
        self.scope = scope
        self.user_id = scope.user_id
        self.attachment_storage = attachment_storage
        self.contexts = ActiveLearningContextResolver(db)
        self.progress = ProgressStore(db, user_id=self.user_id)
        self.artifacts = ArtifactProgressStore(db, user_id=self.user_id)
        self.library_reads = LibraryReadModel(db, user_id=self.user_id)
        if isinstance(scope, WorkerExecutionContext):
            self._install_worker_fence(scope)

    def _install_worker_fence(
        self,
        context: WorkerExecutionContext,
    ) -> None:
        @event.listens_for(self.db, "before_flush")
        def verify_worker_lease(session, _flush_context, _instances):
            valid = session.connection().execute(
                select(LearningTask.id).where(
                    LearningTask.id == context.task_id,
                    LearningTask.status == "running",
                    LearningTask.lease_owner == context.lease_owner,
                    LearningTask.lease_token == context.lease_token,
                )
            ).scalar_one_or_none()
            if not valid:
                raise AppError(
                    "任务租约已失效，拒绝旧 Worker 写入结果",
                    code="TASK_LEASE_LOST",
                    status=409,
                )

    def ensure_seed(self):
        persona = LOCAL_DEMO_PERSONAS_BY_USER_ID.get(self.user_id)
        user = self.db.get(User, self.user_id)
        if not user:
            user = User(
                id=self.user_id,
                name=persona.display_name if persona else "学习者",
            )
            self.db.add(user)
            self.db.flush()
        shelf = self.db.scalar(
            select(Shelf).where(Shelf.user_id == self.user_id)
        )
        if not shelf:
            self.db.add(
                Shelf(
                    id=(
                        "shelf_technology"
                        if self.user_id == DEMO_USER_ID
                        else (
                            f"shelf_{persona.username.replace('-', '_')}"
                            if persona
                            else uid("shelf")
                        )
                    ),
                    user_id=self.user_id,
                    name=persona.shelf_name if persona else "技术",
                    domain=persona.domain if persona else "计算机",
                    specialty=persona.specialty if persona else "软件工程",
                    tags_json=(
                        dump(list(persona.tags))
                        if persona
                        else '["AI","云原生"]'
                    ),
                )
            )
        self.db.commit()

    def shelf(self, shelf_id):
        row = self.db.scalar(select(Shelf).where(Shelf.id == shelf_id, Shelf.user_id == self.user_id))
        if not row:
            raise AppError("书架不存在", code="SHELF_NOT_FOUND", status=404)
        return row

    def bootstrap(self):
        view = self.library_reads.bootstrap()
        view["resume"] = self.resume_position()
        return view

    def resume_position(self):
        row = self.db.scalar(
            select(LearningResumePosition)
            .where(LearningResumePosition.user_id == self.user_id)
            .order_by(LearningResumePosition.updated_at.desc())
        )
        if not row:
            return None
        return {
            "learningRunId": row.learning_run_id,
            "sectionId": row.section_id,
            "blockId": row.block_id,
            "updatedAt": timestamp(row.updated_at),
        }

    def record_resume_position(self, section_id: str, block_id: str):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(context.series.id)
        row = self.db.scalar(
            select(LearningResumePosition).where(
                LearningResumePosition.user_id == self.user_id,
                LearningResumePosition.learning_run_id == learning_run.id,
            )
        )
        if not row:
            row = LearningResumePosition(
                id=uid("resume"),
                user_id=self.user_id,
                learning_run_id=learning_run.id,
                section_id=section_id,
                block_id=block_id,
            )
            self.db.add(row)
        else:
            row.section_id = section_id
            row.block_id = block_id
            row.updated_at = now()
        self.db.commit()
        return {
            "learningRunId": row.learning_run_id,
            "sectionId": row.section_id,
            "blockId": row.block_id,
            "updatedAt": timestamp(row.updated_at),
        }

    def _shelf(self, shelf):
        return next(
            item
            for item in self.library_reads.bootstrap()["shelves"]
            if item["id"] == shelf.id
        )

    def create_shelf(self, body):
        row = Shelf(
            id=uid("shelf"),
            user_id=self.user_id,
            name=body.name,
            domain=body.domain,
            specialty=body.specialty,
            tags_json=dump(body.tags),
        )
        self.db.add(row)
        self.db.commit()
        return self._shelf(row)

    async def create_plan(self, body, idempotency_key: str | None = None):
        self.shelf(body.shelf_id)
        request = body.model_dump(by_alias=False)
        request_key = (idempotency_key or uid("plan_request")).strip()
        if len(request_key) < 8 or len(request_key) > 128:
            raise AppError("创建请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        reservation_key = (request_key, self.user_id)
        reservation = self.db.get(PlanCreationRequest, reservation_key)
        owns_reservation = False
        if not reservation:
            reservation = PlanCreationRequest(
                idempotency_key=request_key,
                user_id=self.user_id,
                request_hash=request_hash,
                status="pending",
            )
            self.db.add(reservation)
            try:
                self.db.commit()
                owns_reservation = True
            except IntegrityError:
                self.db.rollback()
                reservation = self.db.get(PlanCreationRequest, reservation_key)
        if not reservation or reservation.user_id != self.user_id or reservation.request_hash != request_hash:
            raise AppError("创建请求标识已用于其他学习计划", code="IDEMPOTENCY_KEY_REUSED", status=409)
        if reservation.status == "completed" and reservation.series_id:
            return self.series(reservation.series_id)
        if reservation.status == "failed":
            reservation.status = "pending"
            reservation.error_code = ""
            reservation.updated_at = now()
            self.db.commit()
            owns_reservation = True
        elif not owns_reservation:
            raise AppError("相同学习计划正在生成，请勿重复提交", code="PLAN_CREATION_IN_PROGRESS", status=409)
        try:
            memory = self._memory(body.shelf_id)
            self.db.commit()
            generated = await self.ai.plan(request, memory)
        except Exception as error:
            self.db.rollback()
            failed = self.db.get(PlanCreationRequest, reservation_key)
            if failed:
                failed.status = "failed"
                failed.error_code = safe_error_code(error)
                failed.updated_at = now()
                self.db.commit()
            raise
        plan = LearningPlan(
            id=uid("plan"),
            **request,
            assumptions_json=dump(generated.assumptions),
            confidence=generated.confidence,
            status="active",
        )
        series = Series(
            id=uid("series"),
            plan_id=plan.id,
            shelf_id=body.shelf_id,
            title=generated.series_title,
            rationale=generated.rationale,
        )
        self.db.add(plan)
        self.db.flush()
        self.db.add(series)
        self.db.flush()
        reservation.status = "completed"
        reservation.series_id = series.id
        reservation.updated_at = now()
        learning_run = self.progress.create_run(series.id)
        self.db.flush()
        initial_chapter_id = None
        for book_position, item in enumerate(generated.books, 1):
            book = Book(
                id=uid("book"),
                series_id=series.id,
                shelf_id=body.shelf_id,
                position=book_position,
                title=item.title,
                topic=item.topic,
                description=item.description,
                estimated_minutes=item.estimated_minutes,
            )
            self.db.add(book)
            self.db.flush()
            self.progress.add_book(
                learning_run,
                book,
                status="available" if book_position == 1 else "locked",
            )
            capstone = BookCapstone(
                id=uid("capstone"),
                book_id=book.id,
                title=f"《{book.title}》全书大作业",
                brief_json=dump(
                    {
                        "goal": f"综合运用《{book.title}》的关键机制完成一个可复核成果",
                        "deliverables": ["方案或实现", "验证记录", "边界与复盘"],
                    }
                ),
            )
            self.db.add(capstone)
            self.artifacts.add(
                learning_run_id=learning_run.id,
                target_type="book_capstone",
                target_id=capstone.id,
            )
            for chapter_position, chapter in enumerate(item.chapters, 1):
                chapter_row = Chapter(
                    id=uid("chapter"),
                    book_id=book.id,
                    position=chapter_position,
                    title=chapter.title,
                    objective=chapter.objective,
                )
                self.db.add(chapter_row)
                self.progress.add_chapter(
                    learning_run,
                    chapter_row,
                    status=(
                        "available"
                        if book_position == 1 and chapter_position == 1
                        else "locked"
                    ),
                )
                if book_position == 1 and chapter_position == 1:
                    initial_chapter_id = chapter_row.id
        if initial_chapter_id:
            self.db.add(
                LearningTask(
                    id=uid("task"),
                    learning_run_id=learning_run.id,
                    section_id=None,
                    user_id=self.user_id,
                    task_type="initial_book_preload",
                    idempotency_key=f"initial-book:{series.id}",
                    trigger_id=plan.id,
                    payload_json=dump({"chapterId": initial_chapter_id}),
                    status="pending",
                )
            )
        self.db.commit()
        return self.series(series.id)

    def series(self, series_id):
        view = self.library_reads.series(series_id)
        task = self.db.scalar(
            select(LearningTask)
            .join(LearningRun, LearningRun.id == LearningTask.learning_run_id)
            .where(
                LearningRun.series_id == series_id,
                LearningTask.user_id == self.user_id,
                LearningTask.task_type == "initial_book_preload",
            )
            .order_by(LearningTask.created_at.desc())
        )
        view["initializationTask"] = task_view(task) if task else None
        return view

    def delete_series(self, series_id):
        series = self.contexts.resolve_series(user_id=self.user_id, series_id=series_id).series
        series.deleted_at = now()
        plan = self.db.get(LearningPlan, series.plan_id)
        if plan:
            plan.status = "deleted"
        self.db.commit()

    def _book_progress(self, book):
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id)).all()
        section_units = 0.0
        for chapter in chapters:
            sections = self.db.scalars(select(Section).where(Section.chapter_id == chapter.id)).all()
            denominator = len(sections) if sections else EXPECTED_SECTIONS_PER_CHAPTER
            section_units += sum(
                self.progress.for_section(item, chapter, book).status == "completed"
                for item in sections
            ) / denominator
        chapter_ratio = section_units / len(chapters) if chapters else 0
        return chapter_ratio

    def _book_practice_progress(self, book):
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id)).all()
        practices = self.db.scalars(
            select(ChapterPractice).join(Chapter, Chapter.id == ChapterPractice.chapter_id).where(Chapter.book_id == book.id)
        ).all()
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book.id))
        run = self.progress.active_run(book.series_id)
        total = len(chapters) + 1
        done = sum(
            self.artifacts.for_target(
                learning_run_id=run.id,
                target_type="chapter_practice",
                target_id=item.id,
            ).status
            == "completed"
            for item in practices
        ) + int(
            bool(
                capstone
                and self.artifacts.for_target(
                    learning_run_id=run.id,
                    target_type="book_capstone",
                    target_id=capstone.id,
                ).status
                == "completed"
            )
        )
        return done / total if total else 0

    def book(self, book_id):
        return self.library_reads.book(book_id)

    def delete_book(self, book_id):
        book = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id).book
        deleted_at = now()
        book.deleted_at = deleted_at
        self.db.add(
            ChapterRevision(
                id=uid("revision"),
                book_id=book.id,
                action="book_soft_delete",
                before_json=dump(
                    {
                        "id": book.id,
                        "seriesId": book.series_id,
                        "position": book.position,
                        "title": book.title,
                        "status": self.progress.for_book(book).status,
                    }
                ),
                after_json=dump({"deletedAt": timestamp(deleted_at)}),
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
        elif not any(self.progress.for_book(item).status != "locked" for item in remaining):
            first_book = remaining[0]
            self.progress.set_status(self.progress.for_book(first_book), "available")
            first_chapter = self.db.scalar(
                select(Chapter)
                .where(Chapter.book_id == first_book.id)
                .order_by(Chapter.position)
            )
            if first_chapter:
                first_chapter_progress = self.progress.for_chapter(
                    first_chapter,
                    first_book,
                )
                if first_chapter_progress.status == "locked":
                    self.progress.set_status(first_chapter_progress, "available")
            first_section = self.db.scalar(
                select(Section)
                .join(Chapter, Chapter.id == Section.chapter_id)
                .where(Chapter.book_id == first_book.id)
                .order_by(Chapter.position, Section.position)
            )
            if first_section and first_chapter:
                first_section_progress = self.progress.for_section(
                    first_section,
                    first_chapter,
                    first_book,
                )
                if first_section_progress.status == "locked":
                    self.progress.set_status(first_section_progress, "available")
        self.db.commit()

    def _chapter(self, chapter):
        sections = self.db.scalars(select(Section).where(Section.chapter_id == chapter.id).order_by(Section.position)).all()
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter.id))
        book = self.db.get(Book, chapter.book_id)
        return {
            "id": chapter.id,
            "position": chapter.position,
            "title": chapter.title,
            "objective": chapter.objective,
            "status": self.progress.for_chapter(chapter, book).status,
            "generated": bool(sections),
            "sections": [self._section_summary(item) for item in sections],
            "practice": self._practice(practice) if practice else None,
        }

    def _section_summary(self, section):
        progress = self.progress.for_section(section)
        return {
            "id": section.id,
            "position": section.position,
            "title": section.title,
            "question": section.question,
            "objectives": load(section.objectives_json, []),
            "status": progress.status,
            "bestScore": progress.best_score,
            "totalScore": progress.total_score,
            "askMeUnlocked": progress.ask_me_unlocked,
        }

    def _assert_future(self, chapter):
        generated = self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == chapter.id))
        if self.progress.for_chapter(chapter).status == "completed" or generated:
            raise AppError("已开始章节不能调整", code="CHAPTER_ALREADY_STARTED", status=409)

    def add_chapter(self, book_id, body):
        book = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id).book
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        started_end = max((item.position for item in chapters if self.progress.for_chapter(item, book).status == "completed" or self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id))), default=0)
        position = body.position or len(chapters) + 1
        if position <= started_end or position > len(chapters) + 1:
            raise AppError("只能在未开始章节范围内新增", code="CHAPTER_POSITION_INVALID", status=409)
        for item in reversed(chapters):
            if item.position >= position:
                item.position += 1000
        self.db.flush()
        for item in chapters:
            if item.position >= 1000:
                item.position -= 999
        row = Chapter(id=uid("chapter"), book_id=book.id, position=position, title=body.title, objective=body.objective)
        self.db.add(row)
        self.progress.add_chapter(
            self.progress.active_run(book.series_id),
            row,
            status="locked",
        )
        self.db.add(ChapterRevision(id=uid("revision"), book_id=book.id, action="add", after_json=dump({"id": row.id, "position": position, "title": row.title})))
        self.db.commit()
        return self._chapter(row)

    def update_chapter(self, chapter_id, body):
        chapter = self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id).chapter
        self._assert_future(chapter)
        before = {"title": chapter.title, "objective": chapter.objective}
        if body.title is not None:
            chapter.title = body.title
        if body.objective is not None:
            chapter.objective = body.objective
        self.db.add(ChapterRevision(id=uid("revision"), book_id=chapter.book_id, action="update", before_json=dump(before), after_json=dump({"title": chapter.title, "objective": chapter.objective})))
        self.db.commit()
        return self._chapter(chapter)

    def delete_chapter(self, chapter_id):
        chapter = self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id).chapter
        self._assert_future(chapter)
        book_id, old_position = chapter.book_id, chapter.position
        count = self.db.scalar(select(func.count()).select_from(Chapter).where(Chapter.book_id == book_id))
        if count <= 1:
            raise AppError("一本书至少保留一章", code="LAST_CHAPTER", status=409)
        self.db.add(ChapterRevision(id=uid("revision"), book_id=book_id, action="delete", before_json=dump({"id": chapter.id, "position": old_position, "title": chapter.title})))
        chapter_progress = self.db.scalar(
            select(ChapterProgress).where(
                ChapterProgress.user_id == self.user_id,
                ChapterProgress.chapter_id == chapter.id,
            )
        )
        if chapter_progress:
            self.db.delete(chapter_progress)
            self.db.flush()
        self.db.delete(chapter)
        self.db.flush()
        later = self.db.scalars(select(Chapter).where(Chapter.book_id == book_id, Chapter.position > old_position).order_by(Chapter.position)).all()
        for item in later:
            item.position += 1000
        self.db.flush()
        for item in later:
            item.position -= 1001
        self.db.commit()

    def reorder_chapters(self, book_id, chapter_ids):
        self.contexts.resolve_book(user_id=self.user_id, book_id=book_id)
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.position)).all()
        book = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id).book
        future = [item for item in chapters if not self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id)) and self.progress.for_chapter(item, book).status != "completed"]
        if set(chapter_ids) != {item.id for item in future} or len(chapter_ids) != len(future):
            raise AppError("排序必须且只能包含全部未开始章节", code="CHAPTER_ORDER_INVALID", status=409)
        slots = sorted(item.position for item in future)
        by_id = {item.id: item for item in future}
        before = [item.id for item in sorted(future, key=lambda value: value.position)]
        for item in future:
            item.position += 1000
        self.db.flush()
        for position, chapter_id in zip(slots, chapter_ids, strict=True):
            by_id[chapter_id].position = position
        self.db.add(ChapterRevision(id=uid("revision"), book_id=book_id, action="reorder", before_json=dump(before), after_json=dump(chapter_ids)))
        self.db.commit()
        return self.book(book_id)

    async def replan_chapters(self, book_id):
        book = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id).book
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        started, future = [], []
        for item in chapters:
            target = started if self.progress.for_chapter(item, book).status == "completed" or self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id)) else future
            target.append(item)
        before = [{"id": item.id, "title": item.title, "objective": item.objective, "position": item.position} for item in future]
        request = {
            "title": book.title,
            "topic": book.topic,
            "description": book.description,
            "started_chapters": [{"title": item.title, "objective": item.objective} for item in started],
            "future_chapters": [{"title": item.title, "objective": item.objective} for item in future],
        }
        memory = self._memory(book.shelf_id)
        self.db.commit()
        generated = await self.ai.replan_book(request, memory)
        proposal = ChapterRevision(
            id=uid("revision"),
            book_id=book.id,
            action="ai_replan_proposal",
            before_json=dump(before),
            after_json=dump({"rationale": generated.rationale, "chapters": [item.model_dump() for item in generated.chapters]}),
        )
        self.db.add(proposal)
        self.db.commit()
        return {"proposalId": proposal.id, "rationale": generated.rationale, "chapters": [item.model_dump() for item in generated.chapters], "requiresConfirmation": True}

    def confirm_replan(self, book_id, proposal_id):
        book = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id).book
        proposal = self.db.get(ChapterRevision, proposal_id)
        if not proposal or proposal.book_id != book_id or proposal.action != "ai_replan_proposal":
            raise AppError("重规划提案不存在", code="REPLAN_PROPOSAL_NOT_FOUND", status=404)
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        started, future = [], []
        for item in chapters:
            target = started if self.progress.for_chapter(item, book).status == "completed" or self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id)) else future
            target.append(item)
        current = [{"id": item.id, "title": item.title, "objective": item.objective, "position": item.position} for item in future]
        if current != load(proposal.before_json, []):
            raise AppError("未来章节已变化，请重新生成提案", code="REPLAN_PROPOSAL_STALE", status=409)
        proposed = load(proposal.after_json, {})
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
        for offset, item in enumerate(proposed["chapters"], len(started) + 1):
            chapter = Chapter(id=uid("chapter"), book_id=book.id, position=offset, title=item["title"], objective=item["objective"])
            self.db.add(chapter)
            self.progress.add_chapter(
                self.progress.active_run(book.series_id),
                chapter,
                status="locked",
            )
        proposal.action = "ai_replan_confirmed"
        self.db.commit()
        return self.book(book.id)

    async def generate_chapter(self, chapter_id):
        chapter_context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        if self.db.scalar(
            select(func.count())
            .select_from(Section)
            .where(Section.chapter_id == chapter_id)
        ):
            return self._chapter(chapter_context.chapter)
        resource_key = f"chapter:{chapter_id}"
        owner_id = acquire_generation_lease(self.db, resource_key)
        if owner_id is None:
            raise AppError(
                "本章正在生成，请等待当前任务完成",
                code="GENERATION_IN_PROGRESS",
                status=409,
            )
        try:
            return await self._generate_chapter_locked(
                chapter_id,
                resource_key,
                owner_id,
            )
        finally:
            release_generation_lease(self.db, resource_key, owner_id)

    def _renew_generation_lease(self, resource_key, owner_id):
        if not renew_generation_lease(self.db, resource_key, owner_id):
            raise AppError(
                "生成租约已经被新的请求接管，旧结果不会保存",
                code="GENERATION_LEASE_LOST",
                status=409,
            )

    async def _generate_chapter_locked(
        self,
        chapter_id,
        resource_key,
        owner_id,
    ):
        chapter_context = self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id)
        chapter = chapter_context.chapter
        if self.progress.for_chapter(chapter, chapter_context.book).status == "locked":
            raise AppError("请先完成前置学习", code="CHAPTER_LOCKED", status=403)
        if not self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == chapter.id)):
            book = chapter_context.book
            request = {"title": chapter.title, "objective": chapter.objective}
            memory = self._memory(book.shelf_id)
            self.db.commit()
            generated = await self.ai.chapter(request, memory)
            self._renew_generation_lease(resource_key, owner_id)
            chapter_context = self.contexts.resolve_chapter(
                user_id=self.user_id,
                chapter_id=chapter_id,
            )
            chapter = chapter_context.chapter
            if self.db.scalar(
                select(func.count())
                .select_from(Section)
                .where(Section.chapter_id == chapter.id)
            ):
                self.db.commit()
                return self._chapter(chapter)
            for position, item in enumerate(generated.sections, 1):
                section = Section(
                    id=uid("section"),
                    chapter_id=chapter.id,
                    position=position,
                    title=item.title,
                    question=item.question,
                    objectives_json=dump(item.objectives),
                )
                self.db.add(section)
                self.progress.add_section(
                    self.progress.active_run(chapter_context.series.id),
                    section,
                    status="available" if position == 1 else "locked",
                )
            practice = ChapterPractice(
                id=uid("practice"),
                chapter_id=chapter.id,
                title=f"{chapter.title}：章末实践",
                instructions_json=dump(
                    {
                        "objective": chapter.objective,
                        "steps": ["完成一个最小实践", "保存输入、输出或截图证据", "记录失败边界与复盘"],
                    }
                ),
            )
            self.db.add(practice)
            self.artifacts.add(
                learning_run_id=self.progress.active_run(chapter_context.series.id).id,
                target_type="chapter_practice",
                target_id=practice.id,
            )
            self.db.commit()
        return self._chapter(chapter)

    async def generate_section(self, section_id, retry=False, retry_attempt_id=None):
        resource_key = f"section:{section_id}"
        owner_id = acquire_generation_lease(self.db, resource_key)
        if owner_id is None:
            if not retry and self.db.scalar(
                select(ContentVersion)
                .where(ContentVersion.section_id == section_id)
                .order_by(ContentVersion.version.desc())
            ):
                return self.section(section_id)
            raise AppError(
                "本节正在生成，请等待当前任务完成",
                code="GENERATION_IN_PROGRESS",
                status=409,
            )
        try:
            return await self._generate_section_locked(
                section_id,
                retry=retry,
                retry_attempt_id=retry_attempt_id,
                resource_key=resource_key,
                owner_id=owner_id,
            )
        finally:
            release_generation_lease(self.db, resource_key, owner_id)

    async def _generate_section_locked(
        self,
        section_id,
        retry=False,
        retry_attempt_id=None,
        resource_key=None,
        owner_id=None,
    ):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = section_context.section
        if self.progress.for_section(
            section,
            section_context.chapter,
            section_context.book,
        ).status == "locked":
            raise AppError("小节未解锁", code="SECTION_LOCKED", status=403)
        existing = self.db.scalar(select(ContentVersion).where(ContentVersion.section_id == section.id).order_by(ContentVersion.version.desc()))
        latest_quiz = self.db.scalar(select(QuizSet).where(QuizSet.section_id == section.id).order_by(QuizSet.generation.desc()))
        if existing and not retry:
            return self.section(section.id)
        running = self.db.scalar(select(GenerationRun).where(GenerationRun.section_id == section.id, GenerationRun.status == "running").order_by(GenerationRun.started_at.desc()))
        if running:
            started = running.started_at if running.started_at.tzinfo else running.started_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() < 300:
                raise AppError("本节正在生成，请稍后读取状态", code="GENERATION_IN_PROGRESS", status=409)
            running.status, running.error_code, running.error_message, running.finished_at = "failed", "GENERATION_ABANDONED", "上一次生成超过 5 分钟未完成，已允许安全重试", now()
            self.db.commit()
        attempt = (self.db.scalar(select(func.max(GenerationRun.attempt)).where(GenerationRun.section_id == section.id)) or 0) + 1
        run = GenerationRun(
            id=uid("generation"),
            section_id=section.id,
            operation="remediation" if retry else "lesson",
            attempt=attempt,
            status="running",
            model=getattr(self.ai, "model", ""),
            trace_json=dump({"stage": "model_call", "retry": retry}),
        )
        self.db.add(run)
        self.db.commit()
        try:
            prior = load(latest_quiz.questions_json, []) if retry and latest_quiz else []
            book = self._book_for_section(section)
            remediation_count = self.db.scalar(select(func.count()).select_from(Remediation).where(Remediation.section_id == section.id)) if retry else 0
            remediation_strategy = ["paragraph_locator", "alternative_explanation", "prerequisite_supplement"][min(remediation_count, 2)] if retry else None
            lesson = None
            verification = []
            rejected_source_urls: list[str] = []
            ai_harness_trace: list[dict] = []
            max_generation_attempts = 4
            for novelty_attempt in range(1, max_generation_attempts + 1):
                memory = self._memory(book.shelf_id)
                memory_trace = {"memoryApplied": bool(memory), "memoryConceptCount": len(memory)}
                run.trace_json = dump({"stage": "model_call", "retry": retry, "noveltyAttempt": novelty_attempt, **memory_trace})
                self.db.commit()
                lesson_request = {**self._section_summary(section), "rejectedSourceUrls": rejected_source_urls}
                if retry:
                    lesson_request["remediationStrategy"] = remediation_strategy
                lesson = await self.ai.lesson(lesson_request, memory, prior)
                self._renew_generation_lease(resource_key, owner_id)
                ai_harness_trace = self._ai_harness_trace()
                run.trace_json = dump({"stage": "source_verification", "retry": retry, "noveltyAttempt": novelty_attempt, "sourceUrls": [item.url for item in lesson.sources], "aiHarness": ai_harness_trace, **memory_trace})
                self.db.commit()
                try:
                    verification = await self.source_verifier.verify(lesson.sources)
                    self._renew_generation_lease(resource_key, owner_id)
                except AppError as error:
                    rejected_source_urls.extend(item.url for item in lesson.sources)
                    rejected_source_urls = list(dict.fromkeys(rejected_source_urls))
                    if error.code == "SOURCE_UNREACHABLE" and novelty_attempt < max_generation_attempts:
                        lesson = None
                        continue
                    raise
                if not retry or self._questions_are_novel(prior, [item.model_dump() for item in lesson.questions]):
                    break
                lesson = None
            if lesson is None:
                raise AppError("模型连续返回与旧题实质相同的题集", code="QUIZ_NOT_NOVEL", status=502)
            content = existing
            if not retry:
                content = ContentVersion(
                    id=uid("content"),
                    section_id=section.id,
                    version=(existing.version + 1 if existing else 1),
                    blocks_json="[]",
                    sources_json=dump([item.model_dump() for item in lesson.sources]),
                    confidence=lesson.confidence,
                )
                blocks = []
                for position, block in enumerate(lesson.blocks, 1):
                    payload = block.model_dump()
                    payload["id"] = f"block_{content.id}_{position}"
                    payload["version"] = content.version
                    blocks.append(payload)
                content.blocks_json = dump(blocks)
                self.db.add(content)
                self.db.flush()
                self.db.add(SourceVerification(id=uid("verification"), content_version_id=content.id, report_json=dump(verification)))
            quiz = QuizSet(
                id=uid("quiz"),
                section_id=section.id,
                content_version_id=content.id,
                generation=(latest_quiz.generation + 1 if latest_quiz else 1),
                questions_json=dump([item.model_dump() for item in lesson.questions]),
            )
            self.db.add(quiz)
            self.db.flush()
            if retry:
                if not retry_attempt_id:
                    raise AppError("补救教学必须绑定失败答题", code="REMEDIATION_ATTEMPT_REQUIRED")
                remediation_blocks = []
                for position, block in enumerate(lesson.blocks, 1):
                    payload = block.model_dump()
                    payload["id"] = f"block_remediation_{quiz.id}_{position}"
                    payload["version"] = quiz.generation
                    remediation_blocks.append(payload)
                failed_objectives = sorted(
                    {
                        item["objective"]
                        for item in load(self.db.get(QuizAttempt, retry_attempt_id).results_json, [])
                        if not item["correct"]
                    }
                )
                self.db.add(
                    Remediation(
                        id=uid("remediation"),
                        section_id=section.id,
                        attempt_id=retry_attempt_id,
                        replacement_quiz_id=quiz.id,
                        blocks_json=dump(remediation_blocks),
                        objectives_json=dump(failed_objectives),
                        strategy=remediation_strategy,
                    )
                )
            run.status, run.finished_at = "succeeded", now()
            run.trace_json = dump({"stage": "persisted", "contentVersionId": content.id if content else None, "quizSetId": quiz.id, "sourceVerification": verification, "aiHarness": ai_harness_trace, **memory_trace})
            self.db.commit()
            return self.section(section.id)
        except BaseException as error:
            self.db.rollback()
            operation_id = run.id
            run = self.db.get(GenerationRun, operation_id)
            if run:
                run.status = "failed"
                run.error_code = getattr(error, "code", type(error).__name__)
                run.error_message = (
                    str(error)[:2000]
                    if isinstance(error, AppError)
                    else "生成失败，请稍后重试"
                )
                run.finished_at = now()
                previous_trace = load(run.trace_json, {})
                harness_trace = self._ai_harness_trace()
                run.trace_json = dump({
                    **previous_trace,
                    "stage": "failed",
                    **({"aiHarness": harness_trace} if harness_trace else {}),
                })
                self.db.commit()
            if isinstance(error, AppError):
                if error.operation_id is None:
                    error.operation_id = operation_id
                raise
            if isinstance(error, (CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise AiError(
                "小节生成失败；失败状态已保存，可安全重试",
                operation_id=operation_id,
            ) from error

    def _ai_harness_trace(self) -> list[dict]:
        trace = getattr(self.ai, "structured_trace", None)
        if not callable(trace):
            return []
        value = trace()
        return value if isinstance(value, list) else []

    def _questions_are_novel(self, prior, current):
        if not prior or len(prior) != len(current):
            return False
        if Counter(item["objective"] for item in prior) != Counter(item["objective"] for item in current):
            return False
        prior_by_objective = {}
        for item in prior:
            prior_by_objective.setdefault(item["objective"], []).append(item)
        for question in current:
            candidates = prior_by_objective.get(question["objective"], [])
            if any(
                normalized(question["prompt"]) == normalized(old["prompt"])
                or {normalized(option) for option in question["options"]} == {normalized(option) for option in old["options"]}
                for old in candidates
            ):
                return False
            if question.get("difficulty", "standard") != "standard":
                return False
        return True

    def section(self, section_id):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = section_context.section
        learning_run = self.progress.active_run(section_context.series.id)
        content = self.db.scalar(select(ContentVersion).where(ContentVersion.section_id == section.id).order_by(ContentVersion.version.desc()))
        quiz = self.db.scalar(select(QuizSet).where(QuizSet.section_id == section.id).order_by(QuizSet.generation.desc()))
        note = self.db.scalar(
            select(LearningNote).where(
                LearningNote.section_id == section.id,
                LearningNote.user_id == self.user_id,
                LearningNote.learning_run_id == learning_run.id,
            )
        )
        run = self.db.scalar(select(GenerationRun).where(GenerationRun.section_id == section.id).order_by(GenerationRun.started_at.desc()))
        remediations = self.db.scalars(
            select(Remediation)
            .join(QuizAttempt, QuizAttempt.id == Remediation.attempt_id)
            .where(
                Remediation.section_id == section.id,
                QuizAttempt.learning_run_id == learning_run.id,
            )
            .order_by(Remediation.created_at)
        ).all()
        remediation_runs = self.db.scalars(
            select(GenerationRun)
            .where(
                GenerationRun.section_id == section.id,
                GenerationRun.operation == "remediation",
                GenerationRun.status == "succeeded",
            )
            .order_by(GenerationRun.started_at)
        ).all()
        remediation_run_by_quiz = {
            trace.get("quizSetId"): (item, trace)
            for item in remediation_runs
            if (trace := load(item.trace_json, {})).get("quizSetId")
        }
        workflow_tasks = self.db.scalars(
            select(LearningTask)
            .where(
                LearningTask.learning_run_id == learning_run.id,
                LearningTask.user_id == self.user_id,
                LearningTask.section_id == section.id,
            )
            .order_by(LearningTask.created_at)
        ).all()
        verification = self.db.scalar(select(SourceVerification).where(SourceVerification.content_version_id == content.id)) if content else None
        questions = load(quiz.questions_json, []) if quiz else []
        public = [
            {
                **{key: value for key, value in question.items() if key != "correct"},
                "selectionMode": "multiple" if len(set(question.get("correct", []))) > 1 else "single",
            }
            for question in questions
        ]
        return {
            **self._section_summary(section),
            "generation": self._generation(run) if run else None,
            "content": {
                "id": content.id,
                "version": content.version,
                "blocks": load(content.blocks_json, []),
                "sources": load(content.sources_json, []),
                "sourceVerification": load(verification.report_json, []) if verification else [],
                "confidence": content.confidence,
            }
            if content
            else None,
            "quiz": {"id": quiz.id, "generation": quiz.generation, "questions": public} if quiz else None,
            "remediations": [
                {
                    "id": item.id,
                    "attemptId": item.attempt_id,
                    "replacementQuizId": item.replacement_quiz_id,
                    "blocks": load(item.blocks_json, []),
                    "objectives": load(item.objectives_json, []),
                    "strategy": item.strategy,
                    "sourceVerification": (
                        remediation_run_by_quiz[item.replacement_quiz_id][1].get(
                            "sourceVerification", []
                        )
                        if item.replacement_quiz_id in remediation_run_by_quiz
                        else []
                    ),
                    "sourceLineage": (
                        {
                            "mode": "generation_trace",
                            "generationRunId": remediation_run_by_quiz[
                                item.replacement_quiz_id
                            ][0].id,
                        }
                        if item.replacement_quiz_id in remediation_run_by_quiz
                        else {"mode": "missing", "generationRunId": None}
                    ),
                }
                for item in remediations
            ],
            "note": self._note(note) if note else None,
            "workflowTasks": [task_view(task) for task in workflow_tasks],
        }

    def _generation(self, run):
        return {
            "id": run.id,
            "operation": run.operation,
            "attempt": run.attempt,
            "status": run.status,
            "model": run.model,
            "trace": load(run.trace_json, {}),
            "errorCode": run.error_code or None,
            "error": run.error_message or None,
            "startedAt": timestamp(run.started_at),
            "finishedAt": timestamp(run.finished_at),
        }

    async def submit_quiz(self, section_id, body, idempotency_key=None):
        return await SubmitQuiz(
            self.db,
            user_id=self.user_id,
        ).execute(section_id, body, idempotency_key)

    async def execute_learning_task(
        self,
        execution: WorkerExecutionContext,
    ):
        if not isinstance(self.scope, WorkerExecutionContext):
            raise AppError(
                "后台任务不能通过用户请求上下文执行",
                code="WORKER_SCOPE_REQUIRED",
                status=403,
            )
        if self.scope != execution:
            raise AppError(
                "Worker 执行上下文不匹配",
                code="WORKER_SCOPE_MISMATCH",
                status=403,
            )
        task = self.db.scalar(
            select(LearningTask).where(
                LearningTask.id == execution.task_id,
                LearningTask.user_id == self.user_id,
                LearningTask.status == "running",
                LearningTask.lease_owner == execution.lease_owner,
                LearningTask.lease_token == execution.lease_token,
            )
        )
        if not task:
            raise AppError(
                "学习任务不存在",
                code="LEARNING_TASK_NOT_FOUND",
                status=404,
            )
        try:
            payload = load(task.payload_json, {})
            if task.task_type == "initial_book_preload":
                aggregate = self.contexts.resolve_chapter_learning_task(
                    user_id=self.user_id,
                    task_id=task.id,
                    chapter_id=payload.get("chapterId"),
                )
            else:
                aggregate = self.contexts.resolve_learning_task(
                    user_id=self.user_id,
                    task_id=task.id,
                )
            task = aggregate.task
            if task.task_type == "initial_book_preload":
                result = await self._preload_initial_book(
                    aggregate.chapter.id
                )
            elif task.task_type == "note_generation":
                context = self.contexts.resolve_section(
                    user_id=task.user_id,
                    section_id=task.section_id,
                )
                await self._ensure_note(context.section)
                note = self.db.scalar(
                    select(LearningNote).where(
                        LearningNote.learning_run_id == task.learning_run_id,
                        LearningNote.user_id == task.user_id,
                        LearningNote.section_id == task.section_id,
                    )
                )
                result = {"noteId": note.id if note else None}
            elif task.task_type == "remediation_generation":
                attempt_id = payload.get("attemptId")
                existing_remediation = self.db.scalar(
                    select(Remediation)
                    .join(QuizAttempt, QuizAttempt.id == Remediation.attempt_id)
                    .where(
                        Remediation.attempt_id == attempt_id,
                        Remediation.section_id == task.section_id,
                        QuizAttempt.learning_run_id == task.learning_run_id,
                        QuizAttempt.user_id == task.user_id,
                    )
                )
                if existing_remediation:
                    result = {
                        "quizSetId": existing_remediation.replacement_quiz_id,
                        "remediationId": existing_remediation.id,
                    }
                else:
                    view = await self.generate_section(
                        task.section_id,
                        retry=True,
                        retry_attempt_id=attempt_id,
                    )
                    generated_remediation = next(
                        (
                            item
                            for item in reversed(view["remediations"])
                            if item["attemptId"] == attempt_id
                        ),
                        None,
                    )
                    if not generated_remediation:
                        raise AppError(
                            "补救教学生成完成但未找到对应结果",
                            code="REMEDIATION_RESULT_MISSING",
                            status=500,
                        )
                    result = {
                        "quizSetId": generated_remediation["replacementQuizId"],
                        "remediationId": generated_remediation["id"],
                    }
            elif task.task_type == "next_section_preload":
                result = await self._preload_next_section(
                    payload.get("sourceSectionId") or task.section_id
                )
            else:
                raise AppError(
                    "不支持的学习任务类型",
                    code="LEARNING_TASK_TYPE_UNSUPPORTED",
                    status=500,
                )
            return task_view(complete_task(self.db, execution, result))
        except CancelledError:
            release_task(self.db, execution)
            raise
        except Exception as error:
            return task_view(fail_task(self.db, execution, error))

    async def _preload_initial_book(self, chapter_id):
        if not chapter_id:
            raise AppError(
                "首节预生成任务缺少章节",
                code="INITIAL_CHAPTER_MISSING",
                status=500,
            )
        await self.generate_chapter(chapter_id)
        target = self.db.scalar(
            select(Section)
            .where(Section.chapter_id == chapter_id)
            .order_by(Section.position)
        )
        if not target:
            raise AppError(
                "首章没有可生成的小节",
                code="INITIAL_SECTION_MISSING",
                status=500,
            )
        await self.generate_section(target.id)
        return {"targetSectionId": target.id}

    async def _preload_next_section(self, source_section_id):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=source_section_id,
        )
        target = self.db.scalar(
            select(Section)
            .where(
                Section.chapter_id == context.chapter.id,
                Section.position > context.section.position,
            )
            .order_by(Section.position)
        )
        if not target:
            next_chapter = self.db.scalar(
                select(Chapter)
                .where(
                    Chapter.book_id == context.book.id,
                    Chapter.position > context.chapter.position,
                )
                .order_by(Chapter.position)
            )
            if not next_chapter:
                next_book = self.db.scalar(
                    select(Book)
                    .where(
                        Book.series_id == context.book.series_id,
                        Book.position > context.book.position,
                        Book.deleted_at.is_(None),
                    )
                    .order_by(Book.position)
                )
                next_chapter = (
                    self.db.scalar(
                        select(Chapter)
                        .where(Chapter.book_id == next_book.id)
                        .order_by(Chapter.position)
                    )
                    if next_book
                    else None
                )
            if next_chapter:
                await self.generate_chapter(next_chapter.id)
                target = self.db.scalar(
                    select(Section)
                    .where(Section.chapter_id == next_chapter.id)
                    .order_by(Section.position)
                )
        if not target:
            return {"targetSectionId": None, "endOfSeries": True}
        await self.generate_section(target.id)
        return {"targetSectionId": target.id, "endOfSeries": False}

    def learning_task(self, task_id):
        task = self.db.scalar(
            select(LearningTask).where(
                LearningTask.id == task_id,
                LearningTask.user_id == self.user_id,
            )
        )
        if not task:
            raise AppError(
                "学习任务不存在",
                code="LEARNING_TASK_NOT_FOUND",
                status=404,
            )
        return task_view(task)

    def retry_learning_task(self, task_id):
        task = self.db.scalar(
            select(LearningTask).where(
                LearningTask.id == task_id,
                LearningTask.user_id == self.user_id,
            )
        )
        if not task:
            raise AppError(
                "学习任务不存在",
                code="LEARNING_TASK_NOT_FOUND",
                status=404,
            )
        return task_view(reset_failed_task(self.db, task))

    def retry_note_task(self, task_id):
        task = self.db.scalar(
            select(LearningTask).where(
                LearningTask.id == task_id,
                LearningTask.user_id == self.user_id,
                LearningTask.task_type == "note_generation",
            )
        )
        if not task:
            raise AppError("笔记任务不存在", code="NOTE_TASK_NOT_FOUND", status=404)
        return task_view(reset_failed_task(self.db, task))

    def _add_evidence(self, context, concept, evidence_type, result, delta):
        evidence = LearningEvidence(
            id=uid("evidence"),
            learning_run_id=self.progress.active_run(context["series"].id).id,
            user_id=self.user_id,
            shelf_id=context["shelf"].id,
            series_id=context["series"].id,
            book_id=context["book"].id,
            chapter_id=context["chapter"].id,
            section_id=context["section"].id,
            concept=concept[:300],
            evidence_type=evidence_type,
            result_json=dump(result),
            mastery_delta=delta,
        )
        self.db.add(evidence)
        memory = self.db.scalar(select(LearningMemory).where(LearningMemory.user_id == self.user_id, LearningMemory.shelf_id == context["shelf"].id, LearningMemory.concept == concept[:300]))
        if not memory:
            memory = LearningMemory(id=uid("memory"), user_id=self.user_id, shelf_id=context["shelf"].id, concept=concept[:300], mastery_score=0, evidence_count=0, summary="")
            self.db.add(memory)
        memory.mastery_score = max(0, min(100, memory.mastery_score + delta))
        memory.evidence_count += 1
        memory.summary = f"{memory.evidence_count} 条证据，当前掌握度 {memory.mastery_score}/100；最近证据：{evidence_type}"
        memory.updated_at = now()

    def _memory(self, shelf_id, limit=30):
        rows = self.db.scalars(select(LearningMemory).where(LearningMemory.user_id == self.user_id, LearningMemory.shelf_id == shelf_id).order_by(LearningMemory.updated_at.desc()).limit(limit)).all()
        return [{"concept": item.concept, "mastery": item.mastery_score, "evidenceCount": item.evidence_count, "summary": item.summary} for item in rows]

    def learning_memory(self, shelf_id=None):
        if shelf_id:
            self.shelf(shelf_id)
            return self._memory(shelf_id, 200)
        shelves = self.db.scalars(select(Shelf).where(Shelf.user_id == self.user_id)).all()
        return {item.id: self._memory(item.id, 200) for item in shelves}

    async def _ensure_note(self, section):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section.id,
        )
        await GenerateLearningNote(
            self.db,
            user_id=self.user_id,
            learning_run_id=self.progress.active_run(context.series.id).id,
            tutor=self.ai,
            section_reader=self.section,
        ).execute(section)

    def _note(self, note):
        return {"id": note.id, "aiContent": load(note.ai_content_json, {}), "userContent": load(note.user_content_json, {}), "version": note.version}

    def update_note(self, section_id, content):
        context = self.contexts.resolve_section(user_id=self.user_id, section_id=section_id)
        learning_run = self.progress.active_run(context.series.id)
        note = self.db.scalar(
            select(LearningNote).where(
                LearningNote.section_id == section_id,
                LearningNote.user_id == self.user_id,
                LearningNote.learning_run_id == learning_run.id,
            )
        )
        if not note:
            raise AppError("笔记不存在", code="NOTE_NOT_FOUND", status=404)
        note.user_content_json, note.version, note.updated_at = dump(content), note.version + 1, now()
        self.db.commit()
        return self._note(note)

    def prepare_ask(self, section_id, body):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(section_context.series.id)
        section_view = self.section(section_id)
        if not section_view["content"]:
            raise AppError("请先生成本节", code="SECTION_NOT_GENERATED")
        valid_blocks = {item["id"] for item in section_view["content"]["blocks"]}
        if body.block_id not in valid_blocks:
            raise AppError("内容块不存在或版本已失效", code="BLOCK_INVALID", status=409)
        session = self.db.scalar(
            select(QaSession).where(
                QaSession.section_id == section_id,
                QaSession.user_id == self.user_id,
                QaSession.learning_run_id == learning_run.id,
            )
        )
        if not session:
            session = QaSession(
                id=uid("qa"),
                learning_run_id=learning_run.id,
                section_id=section_id,
                user_id=self.user_id,
            )
            self.db.add(session)
            self.db.commit()
        messages = self.db.scalars(select(QaMessage).where(QaMessage.session_id == session.id).order_by(QaMessage.created_at)).all()
        threads = self.db.scalars(select(QaThread).where(QaThread.session_id == session.id).order_by(QaThread.updated_at.desc())).all()
        current_history = [
            {"role": item.role, "content": item.content, "blockId": item.block_id}
            for item in messages
            if body.thread_id and item.thread_id == body.thread_id
        ]
        related_summaries = [
            {"threadId": item.thread_id, "summary": item.summary}
            for item in threads
            if item.thread_id != body.thread_id and item.summary
        ][:5]
        suggested = uid("thread")
        return {
            "session": session,
            "suggestedThreadId": suggested,
            "request": {
                "section": section_view,
                "anchorBlockId": body.block_id,
                "question": body.question,
                "requestedThreadId": body.thread_id,
                "forcedRelation": body.force_relation,
                "newThreadId": suggested,
                "weightedContext": {
                    "currentThreadFullHistory": current_history,
                    "relatedThreadSummaries": related_summaries,
                    "crossSectionMemory": self._memory(section_context.book.shelf_id, 10),
                },
            },
        }

    def _save_qa_answer(self, context, body, answer, suggested_relation, thread_summary=""):
        session = context["session"]
        suggested = context["suggestedThreadId"]
        relation = body.force_relation or suggested_relation
        if relation == "follow_up" and body.thread_id:
            thread_id = body.thread_id
        else:
            relation, thread_id = "new_question", suggested
        thread = self.db.scalar(select(QaThread).where(QaThread.session_id == session.id, QaThread.thread_id == thread_id))
        if not thread:
            thread = QaThread(id=uid("qathread"), session_id=session.id, thread_id=thread_id, classification=relation)
            self.db.add(thread)
        thread_summary = thread_summary.strip() or answer.strip()[:240]
        thread.summary, thread.updated_at = thread_summary, now()
        self.db.add_all(
            [
                QaMessage(id=uid("msg"), session_id=session.id, thread_id=thread_id, block_id=body.block_id, role="user", content=body.question),
                QaMessage(id=uid("msg"), session_id=session.id, thread_id=thread_id, block_id=body.block_id, role="assistant", content=answer),
            ]
        )
        memory = load(session.memory_json, {"threads": {}}) or {"threads": {}}
        memory.setdefault("threads", {})[thread_id] = thread_summary
        memory["lastThread"] = thread_id
        session.memory_json = dump(memory)
        self.db.commit()
        return {"sessionId": session.id, "threadId": thread_id, "relation": relation, "answer": answer, "classificationCorrectable": True}

    async def ask(self, section_id, body):
        context = self.prepare_ask(section_id, body)
        self.db.commit()
        result = await self.ai.answer(context["request"])
        return self._save_qa_answer(context, body, result.answer, result.relation, result.thread_summary)

    async def ask_stream(self, context, body):
        parts = []
        self.db.commit()
        stream_answer = getattr(self.ai, "answer_stream", None)
        if callable(stream_answer):
            async for delta in stream_answer(context["request"]):
                if delta:
                    parts.append(delta)
                    yield {"type": "delta", "delta": delta}
            suggested_relation = "follow_up" if body.thread_id else "new_question"
        else:
            result = await self.ai.answer(context["request"])
            parts.append(result.answer)
            suggested_relation = result.relation
            yield {"type": "delta", "delta": result.answer}
        answer = "".join(parts).strip()
        if not answer:
            raise AiError("答疑模型未返回有效内容")
        saved = self._save_qa_answer(context, body, answer, suggested_relation)
        yield {
            "type": "done",
            "sessionId": saved["sessionId"],
            "threadId": saved["threadId"],
            "relation": saved["relation"],
            "classificationCorrectable": True,
        }

    def correct_qa_classification(self, section_id, thread_id, body):
        context = self.contexts.resolve_section(user_id=self.user_id, section_id=section_id)
        learning_run = self.progress.active_run(context.series.id)
        session = self.db.scalar(
            select(QaSession).where(
                QaSession.section_id == section_id,
                QaSession.user_id == self.user_id,
                QaSession.learning_run_id == learning_run.id,
            )
        )
        thread = self.db.scalar(select(QaThread).where(QaThread.session_id == session.id, QaThread.thread_id == thread_id)) if session else None
        if not thread:
            raise AppError("答疑线程不存在", code="QA_THREAD_NOT_FOUND", status=404)
        if body.relation == "follow_up":
            target = self.db.scalar(select(QaThread).where(QaThread.session_id == session.id, QaThread.thread_id == body.target_thread_id)) if body.target_thread_id else None
            if not target or target.thread_id == thread_id:
                raise AppError("纠正为追问时必须指定另一条已有线程", code="QA_TARGET_INVALID")
            messages = self.db.scalars(select(QaMessage).where(QaMessage.session_id == session.id, QaMessage.thread_id == thread_id)).all()
            for item in messages:
                item.thread_id = target.thread_id
            target.summary = "；".join(value for value in [target.summary, thread.summary] if value)
            target.corrected, target.updated_at = True, now()
            self.db.delete(thread)
            corrected_id = target.thread_id
        else:
            thread.classification, thread.corrected, thread.updated_at = "new_question", True, now()
            corrected_id = thread.thread_id
        self.db.commit()
        return {"threadId": corrected_id, "relation": body.relation, "corrected": True}

    async def ask_me(self, section_id, answer):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = section_context.section
        learning_run = self.progress.active_run(section_context.series.id)
        if not self.progress.for_section(
            section,
            section_context.chapter,
            section_context.book,
        ).ask_me_unlocked:
            raise AppError("小节满分后才解锁 Ask Me", code="ASK_ME_LOCKED", status=403)
        session = self.db.scalar(
            select(AskMeSession).where(
                AskMeSession.section_id == section.id,
                AskMeSession.user_id == self.user_id,
                AskMeSession.learning_run_id == learning_run.id,
            )
        )
        entries = load(session.entries_json, []) if session else []
        dimensions = ["mechanism", "boundary", "transfer"]
        if session and session.status == "completed":
            return self._ask_me(session)
        if not session:
            if answer:
                raise AppError("请先开始 Ask Me 再作答", code="ASK_ME_NOT_STARTED")
            section_view = self.section(section_id)
            self.db.commit()
            turn = None
            for validation_attempt in range(1, 4):
                turn = await self.ai.ask_me({"section": section_view, "dimension": "mechanism", "previousAnswer": None, "finalize": False, "validationAttempt": validation_attempt, "requiredEvaluation": "not_evaluated"})
                if turn.dimension == "mechanism" and turn.evaluation == "not_evaluated":
                    break
            if turn is None or turn.dimension != "mechanism" or turn.evaluation != "not_evaluated":
                raise AiError("Ask Me 首轮结构无效")
            session = AskMeSession(id=uid("askme"), learning_run_id=learning_run.id, section_id=section.id, user_id=self.user_id, round_index=0, entries_json=dump([{"dimension": "mechanism", "prompt": turn.prompt, "answer": None, "evaluation": "not_evaluated", "rationale": ""}]))
            self.db.add(session)
            self.db.commit()
            return self._ask_me(session)
        if not answer:
            raise AppError("本轮回答不能为空", code="ASK_ME_ANSWER_REQUIRED")
        current = session.round_index
        current_dimension = dimensions[current]
        finalize = current == 2
        requested_dimension = current_dimension if finalize else dimensions[current + 1]
        section_view = self.section(section_id)
        self.db.commit()
        turn = None
        for validation_attempt in range(1, 4):
            turn = await self.ai.ask_me(
                {
                    "section": section_view,
                    "dimension": requested_dimension,
                    "evaluatesDimension": current_dimension,
                    "previousPrompt": entries[current]["prompt"],
                    "previousAnswer": answer,
                    "priorRounds": entries,
                    "finalize": finalize,
                    "validationAttempt": validation_attempt,
                    "requiredEvaluation": ["strong", "partial", "weak"],
                }
            )
            if turn.dimension == requested_dimension and turn.evaluation != "not_evaluated":
                break
        if turn is None or turn.evaluation == "not_evaluated":
            raise AiError("Ask Me 作答后必须给出能力评估")
        entries[current].update({"answer": answer, "evaluation": turn.evaluation, "rationale": turn.rationale})
        delta = {"strong": 20, "partial": 8, "weak": -5}[turn.evaluation]
        self._add_evidence(self._context(section), f"{section.title}:{current_dimension}", "ask_me", {"dimension": current_dimension, "evaluation": turn.evaluation}, delta)
        if finalize:
            session.status = "completed"
        else:
            if turn.dimension != requested_dimension:
                raise AiError("Ask Me 轮次顺序无效")
            entries.append({"dimension": requested_dimension, "prompt": turn.prompt, "answer": None, "evaluation": "not_evaluated", "rationale": ""})
            session.round_index += 1
        session.entries_json, session.updated_at = dump(entries), now()
        self.db.commit()
        return self._ask_me(session)

    def _ask_me(self, session):
        entries = load(session.entries_json, [])
        return {
            "id": session.id,
            "status": session.status,
            "round": session.round_index + 1,
            "dimension": entries[session.round_index]["dimension"] if entries else "mechanism",
            "prompt": entries[session.round_index]["prompt"] if session.status != "completed" and entries else None,
            "entries": entries,
        }

    def chapter_practice(self, chapter_id):
        self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id)
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter_id))
        if not practice:
            raise AppError("请先生成本章", code="PRACTICE_NOT_GENERATED", status=404)
        return self._practice(practice)

    def upload_chapter_practice_attachment(self, chapter_id, filename, media_type, data):
        context = self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id)
        learning_run = self.progress.active_run(context.series.id)
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter_id))
        if not practice:
            raise AppError("章末实践不存在", code="PRACTICE_NOT_FOUND", status=404)
        if self._practice_progress(practice).status == "locked":
            raise AppError("完成本章后才可上传附件", code="PRACTICE_LOCKED", status=403)
        return self._upload_attachment(
            learning_run.id,
            "chapter_practice",
            practice.id,
            filename,
            media_type,
            data,
        )

    def submit_chapter_practice(self, chapter_id, content, attachment_ids):
        context = self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id)
        learning_run = self.progress.active_run(context.series.id)
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter_id))
        if not practice:
            raise AppError("章末实践不存在", code="PRACTICE_NOT_FOUND", status=404)
        progress = self._practice_progress(practice)
        if progress.status == "locked":
            raise AppError("完成本章后才可提交实践", code="PRACTICE_LOCKED", status=403)
        if not content:
            raise AppError("实践提交不能为空", code="PRACTICE_EMPTY")
        attachments = self._validated_attachments(
            learning_run.id,
            "chapter_practice",
            practice.id,
            attachment_ids,
        )
        attachment_ids = [item.id for item in attachments]
        self.db.add(
            ArtifactSubmission(
                id=uid("artifact_submission"),
                learning_run_id=learning_run.id,
                user_id=self.user_id,
                target_type="chapter_practice",
                target_id=practice.id,
                content_json=dump(content),
                attachment_ids_json=dump(attachment_ids),
            )
        )
        progress.submission_json = dump({"content": content, "attachmentIds": attachment_ids})
        progress.status, progress.updated_at = "completed", now()
        self.db.commit()
        return self._practice(practice)

    def _practice_progress(self, practice):
        chapter = self.db.get(Chapter, practice.chapter_id)
        book = self.db.get(Book, chapter.book_id)
        run = self.progress.active_run(book.series_id)
        return self.artifacts.for_target(
            learning_run_id=run.id,
            target_type="chapter_practice",
            target_id=practice.id,
        )

    def _practice(self, practice):
        progress = self._practice_progress(practice)
        attachments = self._attachments(
            progress.learning_run_id,
            "chapter_practice",
            practice.id,
        )
        return {"id": practice.id, "title": practice.title, "instructions": load(practice.instructions_json, {}), "submission": load(progress.submission_json, {}), "attachments": attachments, "evidenceMode": "file_attachment" if attachments else "structured_only_legacy", "status": progress.status}

    def book_capstone(self, book_id):
        self.contexts.resolve_book(user_id=self.user_id, book_id=book_id)
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book_id))
        if not capstone:
            raise AppError("全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404)
        return self._capstone(capstone)

    def upload_book_capstone_attachment(self, book_id, filename, media_type, data):
        context = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id)
        learning_run = self.progress.active_run(context.series.id)
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book_id))
        if not capstone:
            raise AppError("全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404)
        if self._capstone_progress(capstone).status == "locked":
            raise AppError("完成本书正文后才可上传附件", code="CAPSTONE_LOCKED", status=403)
        return self._upload_attachment(
            learning_run.id,
            "book_capstone",
            capstone.id,
            filename,
            media_type,
            data,
        )

    def submit_book_capstone(self, book_id, content, attachment_ids):
        context = self.contexts.resolve_book(user_id=self.user_id, book_id=book_id)
        learning_run = self.progress.active_run(context.series.id)
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book_id))
        if not capstone:
            raise AppError("全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404)
        progress = self._capstone_progress(capstone)
        if progress.status == "locked":
            raise AppError("完成本书后才可提交大作业", code="CAPSTONE_LOCKED", status=403)
        if not content:
            raise AppError("大作业提交不能为空", code="CAPSTONE_EMPTY")
        attachments = self._validated_attachments(
            learning_run.id,
            "book_capstone",
            capstone.id,
            attachment_ids,
        )
        attachment_ids = [item.id for item in attachments]
        self.db.add(
            ArtifactSubmission(
                id=uid("artifact_submission"),
                learning_run_id=learning_run.id,
                user_id=self.user_id,
                target_type="book_capstone",
                target_id=capstone.id,
                content_json=dump(content),
                attachment_ids_json=dump(attachment_ids),
            )
        )
        progress.submission_json = dump({"content": content, "attachmentIds": attachment_ids})
        progress.status, progress.updated_at = "completed", now()
        self.db.commit()
        return self._capstone(capstone)

    def _capstone_progress(self, capstone):
        book = self.db.get(Book, capstone.book_id)
        run = self.progress.active_run(book.series_id)
        return self.artifacts.for_target(
            learning_run_id=run.id,
            target_type="book_capstone",
            target_id=capstone.id,
        )

    def _capstone(self, capstone):
        progress = self._capstone_progress(capstone)
        attachments = self._attachments(
            progress.learning_run_id,
            "book_capstone",
            capstone.id,
        )
        return {"id": capstone.id, "title": capstone.title, "brief": load(capstone.brief_json, {}), "submission": load(progress.submission_json, {}), "attachments": attachments, "evidenceMode": "file_attachment" if attachments else "structured_only_legacy", "status": progress.status}

    def _upload_attachment(self, learning_run_id, target_type, target_id, filename, media_type, data):
        if not self.attachment_storage:
            raise AppError("附件存储未配置", code="ATTACHMENT_STORAGE_UNAVAILABLE", status=503)
        clean_name = unquote(filename or "attachment.bin").replace("\\", "/").split("/")[-1].strip()
        if not clean_name or len(clean_name) > 255:
            raise AppError("附件文件名无效", code="ATTACHMENT_FILENAME_INVALID")
        attachment_id = uid("attachment")
        self.db.commit()
        stored = self.attachment_storage.store(user_id=self.user_id, target_type=target_type, target_id=target_id, attachment_id=attachment_id, data=data)
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
            self.attachment_storage.resolve(stored.object_key).unlink(missing_ok=True)
            raise
        return self._attachment(attachment)

    def _validated_attachments(self, learning_run_id, target_type, target_id, attachment_ids):
        if not attachment_ids:
            raise AppError("必须提交至少一个真实附件", code="ATTACHMENT_REQUIRED")
        if len(set(attachment_ids)) != len(attachment_ids):
            raise AppError("附件 ID 不得重复", code="ATTACHMENT_DUPLICATE")
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
            raise AppError("附件不存在、无权访问或不属于当前成果", code="ATTACHMENT_INVALID", status=403)
        by_id = {item.id: item for item in attachments}
        return [by_id[item_id] for item_id in attachment_ids]

    def _attachments(self, learning_run_id, target_type, target_id):
        items = self.db.scalars(select(ArtifactAttachment).where(ArtifactAttachment.user_id == self.user_id, ArtifactAttachment.learning_run_id == learning_run_id, ArtifactAttachment.target_type == target_type, ArtifactAttachment.target_id == target_id).order_by(ArtifactAttachment.created_at)).all()
        return [self._attachment(item) for item in items]

    def attachment(self, attachment_id):
        item = self.db.scalar(select(ArtifactAttachment).where(ArtifactAttachment.id == attachment_id, ArtifactAttachment.user_id == self.user_id))
        if not item:
            raise AppError("附件不存在", code="ATTACHMENT_NOT_FOUND", status=404)
        if not self.attachment_storage:
            raise AppError("附件存储未配置", code="ATTACHMENT_STORAGE_UNAVAILABLE", status=503)
        path = self.attachment_storage.resolve(item.object_key)
        if not path.is_file():
            raise AppError("附件对象缺失", code="ATTACHMENT_OBJECT_MISSING", status=410)
        return item, path

    @staticmethod
    def _attachment(item):
        return {"id": item.id, "filename": item.original_filename, "mediaType": item.media_type, "byteSize": item.byte_size, "sha256": item.sha256, "createdAt": timestamp(item.created_at)}

    def _book_for_section(self, section):
        return self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section.id,
        ).book

    def _context(self, section):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section.id,
        )
        return {
            "section": context.section,
            "chapter": context.chapter,
            "book": context.book,
            "series": context.series,
            "shelf": context.shelf,
        }
