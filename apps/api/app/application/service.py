from asyncio import CancelledError
import hashlib
import json
from urllib.parse import unquote
from datetime import timezone
from uuid import uuid4

from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..auth.context import UserScope, WorkerExecutionContext
from ..auth.profile import ProfileService

from ..ai.port import AiPort
from .generation_context import GenerationContextBuilder
from .section_generation import (
    SectionGenerationCoordinator,
    apply_source_repair_scope,
    source_blacklist_from_generation_traces,
)
from ..core.errors import AiError, AppError, safe_error_code
from ..demo_personas import LOCAL_DEMO_PERSONAS_BY_USER_ID
from ..infrastructure.tables import (
    ArtifactAttachment,
    ArtifactSubmission,
    AskMeSession,
    AssessmentObservation,
    AssessmentTarget,
    Book,
    BookCapstone,
    Chapter,
    ChapterProgress,
    ChapterPractice,
    ChapterRevision,
    ContentVersion,
    GenerationRun,
    GovernanceDecisionSnapshot,
    LearningEvidence,
    LearningContractVersion,
    LearningMemory,
    LearningNote,
    LearningNoteReviewSupplement,
    LearningNoteSummary,
    LearningNoteUserRevision,
    LearningRun,
    LearningRunSectionBinding,
    LearningTask,
    LearningPlan,
    LearningMissionVersion,
    LearningResumePosition,
    KnowledgeStateProjection,
    PlanCreationRequest,
    QaMessage,
    QaSession,
    QaThread,
    QuizAttempt,
    QuizSet,
    Remediation,
    Section,
    SectionAssessmentTarget,
    Series,
    Shelf,
    SourceVerification,
    User,
    UserFeedback,
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
from ..modules.learning.milestones import MilestoneService
from ..modules.learning.missions import MissionService
from ..modules.learning.reviews import ReviewAssignmentService
from ..modules.learning.contracts import (
    open_run_section,
)
from ..modules.learning.content_governance_store import (
    governance_view_for_quiz,
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
        self.milestones = MilestoneService(db, user_id=self.user_id, uid=uid)
        self.missions = MissionService(db, user_id=self.user_id, uid=uid)
        self.generation_contexts = GenerationContextBuilder(
            db,
            user_id=self.user_id,
        )
        self.section_generation = SectionGenerationCoordinator(self)
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

    def ensure_user(self):
        """Ensure the authenticated user row exists without creating library data."""
        persona = LOCAL_DEMO_PERSONAS_BY_USER_ID.get(self.user_id)
        user = self.db.get(User, self.user_id)
        if not user:
            user = User(
                id=self.user_id,
                name=persona.display_name if persona else "学习者",
            )
            self.db.add(user)
            self.db.flush()
        self.db.commit()

    def ensure_demo_seed(self):
        """Seed profile and shelf data only for explicit demo identities."""
        persona = LOCAL_DEMO_PERSONAS_BY_USER_ID.get(self.user_id)
        if not persona and self.user_id != DEMO_USER_ID:
            self.ensure_user()
            return

        self.ensure_user()
        ProfileService(self.db, self.user_id).seed_complete(
            profession=persona.display_name if persona else "体验学习者",
            stage="beginner",
            purpose=(
                persona.scenario
                if persona
                else "体验从教材生成到验证和笔记的完整学习闭环"
            ),
            domains=(
                [persona.domain, persona.specialty]
                if persona
                else ["计算机科学"]
            ),
            experience="本地演示账号的预置基础画像",
        )
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
                    origin="demo_seed",
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
        profile = ProfileService(
            self.db,
            self.user_id,
        ).state()["profile"]
        resume = self.resume_position()
        view["profile"] = profile
        view["resume"] = resume
        view["milestoneDashboard"] = self.milestones.dashboard(
            library=view,
            profile=profile,
            resume=resume,
        )
        return view

    def confirm_milestone_path(self, series_id: str):
        return self.milestones.confirm(series_id)

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
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )
        if not binding:
            mission = self.missions.current_version(context.series.id)
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=context.section,
                mission_version_id=mission.id,
                source="resume_block_recovery",
                uid=uid,
                preferred_block_id=block_id or None,
            )
        bound_content = self.db.get(ContentVersion, binding.content_version_id)
        valid_blocks = {
            item.get("id") for item in load(bound_content.blocks_json, [])
        }
        if block_id and block_id not in valid_blocks:
            raise AppError(
                "阅读位置不属于当前正文版本",
                code="RESUME_BLOCK_INVALID",
                status=409,
            )
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
                learning_contract_version_id=(
                    binding.learning_contract_version_id
                ),
                content_version_id=binding.content_version_id,
                block_id=block_id,
            )
            self.db.add(row)
        else:
            row.section_id = section_id
            row.learning_contract_version_id = binding.learning_contract_version_id
            row.content_version_id = binding.content_version_id
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
            origin="user_created",
        )
        self.db.add(row)
        self.db.commit()
        return self._shelf(row)

    async def create_plan(self, body, idempotency_key: str | None = None):
        shelf = self.shelf(body.shelf_id)
        request = body.model_dump(by_alias=False)
        memory = self._memory(body.shelf_id)
        context_pack = self.generation_contexts.build(
            "plan",
            shelf=shelf,
            memory=memory,
            plan_input=request,
        )
        ai_request = self.generation_contexts.attach(request, context_pack)
        request_key = (idempotency_key or uid("plan_request")).strip()
        if len(request_key) < 8 or len(request_key) > 128:
            raise AppError("创建请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        request_hash = hashlib.sha256(
            json.dumps(ai_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
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
            self.db.commit()
            generated = await self.ai.plan(ai_request, memory)
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
        self.db.add(plan)
        self.db.flush()
        mission = self.missions.create_for_plan(
            plan=plan,
            generated=generated,
            learner_context=context_pack.learner.snapshot(),
        )
        series = Series(
            id=uid("series"),
            plan_id=plan.id,
            shelf_id=body.shelf_id,
            title=generated.series_title,
            rationale=generated.rationale,
            initial_mission_version_id=mission.id,
        )
        self.db.add(series)
        self.db.flush()
        reservation.status = "completed"
        reservation.series_id = series.id
        reservation.updated_at = now()
        learning_run = self.progress.create_run(
            series.id,
            initial_mission_version_id=mission.id,
        )
        self.db.flush()
        self.missions.record_initial_adoption(
            run=learning_run,
            mission=mission,
            source="plan_creation",
        )
        initial_chapter_id = None
        milestone_chapters = {}
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
                milestone_chapters[(book_position, chapter_position)] = (
                    chapter_row,
                    book,
                )
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
        self.milestones.create_for_plan(
            series_id=series.id,
            generated=generated,
            chapter_map=milestone_chapters,
        )
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

    def mission(self, series_id: str):
        return self.missions.view(series_id)

    def create_mission_version(self, series_id: str, body):
        result = self.missions.create_draft(series_id, body)
        self.db.commit()
        return result

    def confirm_mission_version(self, series_id: str, mission_version_id: str):
        result = self.missions.confirm(series_id, mission_version_id)
        self.db.commit()
        return result

    def adopt_mission_version(self, series_id: str, body, idempotency_key: str | None):
        result = self.missions.adopt(
            series_id,
            body,
            idempotency_key=idempotency_key,
        )
        self.db.commit()
        return result

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
        book_context = self.contexts.resolve_book(
            user_id=self.user_id,
            book_id=book_id,
        )
        book = book_context.book
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
        mission = self.missions.current_version(book_context.series.id)
        context_pack = self.generation_contexts.build(
            "book_replan",
            shelf=book_context.shelf,
            series=book_context.series,
            book=book,
            mission=mission,
            memory=memory,
        )
        request = self.generation_contexts.attach(request, context_pack)
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

    async def generate_chapter(
        self,
        chapter_id,
        *,
        first_section_status="available",
    ):
        if first_section_status not in {"available", "preparing"}:
            raise AppError(
                "首节准备状态无效",
                code="SECTION_PREPARATION_STATUS_INVALID",
                status=500,
            )
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
                first_section_status,
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
        first_section_status,
    ):
        chapter_context = self.contexts.resolve_chapter(user_id=self.user_id, chapter_id=chapter_id)
        chapter = chapter_context.chapter
        if self.progress.for_chapter(chapter, chapter_context.book).status == "locked":
            raise AppError("请先完成前置学习", code="CHAPTER_LOCKED", status=403)
        if not self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == chapter.id)):
            book = chapter_context.book
            request = {"title": chapter.title, "objective": chapter.objective}
            memory = self._memory(book.shelf_id)
            mission = self.missions.current_version(chapter_context.series.id)
            context_pack = self.generation_contexts.build(
                "chapter",
                shelf=chapter_context.shelf,
                series=chapter_context.series,
                book=book,
                chapter=chapter,
                mission=mission,
                memory=memory,
            )
            request = self.generation_contexts.attach(request, context_pack)
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
                    status=(
                        first_section_status
                        if position == 1
                        else "locked"
                    ),
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

    async def generate_section(
        self,
        section_id,
        retry=False,
        retry_attempt_id=None,
        regenerate=False,
        supersede_remediation_id=None,
        regeneration_feedback=None,
    ):
        resource_key = f"section:{section_id}"
        owner_id = acquire_generation_lease(self.db, resource_key)
        if owner_id is None:
            if not retry and not regenerate and self.db.scalar(
                select(ContentVersion)
                .where(
                    ContentVersion.section_id == section_id,
                    ContentVersion.publication_status == "published",
                )
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
                regenerate=regenerate,
                supersede_remediation_id=supersede_remediation_id,
                regeneration_feedback=regeneration_feedback,
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
        regenerate=False,
        supersede_remediation_id=None,
        regeneration_feedback=None,
        resource_key=None,
        owner_id=None,
    ):
        return await self.section_generation.generate(
            section_id,
            retry=retry,
            retry_attempt_id=retry_attempt_id,
            regenerate=regenerate,
            supersede_remediation_id=supersede_remediation_id,
            regeneration_feedback=regeneration_feedback,
            resource_key=resource_key,
            owner_id=owner_id,
        )

    def open_section(self, section_id: str):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        progress = self.progress.for_section(
            context.section,
            context.chapter,
            context.book,
        )
        if progress.status == "locked":
            raise AppError("小节未解锁", code="SECTION_LOCKED", status=403)
        if progress.status == "preparing":
            raise AppError(
                "下一节正文和验证题仍在准备中",
                code="SECTION_PREPARING",
                status=409,
            )
        run = self.progress.active_run(context.series.id)
        mission = self.missions.current_version(context.series.id)
        open_run_section(
            self.db,
            run=run,
            section=context.section,
            mission_version_id=mission.id,
            source="interactive_open",
            uid=uid,
        )
        self.db.commit()
        return self.section(section_id)

    def section(self, section_id):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = section_context.section
        section_progress = self.progress.for_section(
            section,
            section_context.chapter,
            section_context.book,
        )
        if (
            section_progress.status == "preparing"
            and not isinstance(self.scope, WorkerExecutionContext)
        ):
            raise AppError(
                "下一节正文和验证题仍在准备中",
                code="SECTION_PREPARING",
                status=409,
            )
        learning_run = self.progress.active_run(section_context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        content = (
            self.db.get(ContentVersion, binding.content_version_id)
            if binding
            else self.db.scalar(
                select(ContentVersion)
                .where(
                    ContentVersion.section_id == section.id,
                    ContentVersion.publication_status == "published",
                )
                .order_by(ContentVersion.version.desc())
            )
        )
        if content and content.publication_status != "published":
            content = None
        quiz = (
            self.db.get(QuizSet, binding.initial_quiz_set_id)
            if binding and binding.initial_quiz_set_id
            else self.db.scalar(
                select(QuizSet)
                .where(
                    QuizSet.section_id == section.id,
                    QuizSet.publication_status == "published",
                )
                .order_by(QuizSet.generation.desc())
            )
        )
        if quiz and quiz.publication_status != "published":
            quiz = None
        note = self.db.scalar(
            select(LearningNote).where(
                LearningNote.section_id == section.id,
                LearningNote.user_id == self.user_id,
                LearningNote.learning_run_id == learning_run.id,
            )
        )
        run = self.db.scalar(select(GenerationRun).where(GenerationRun.section_id == section.id).order_by(GenerationRun.started_at.desc()))
        remediation_revisions = self.db.scalars(
            select(Remediation)
            .join(QuizAttempt, QuizAttempt.id == Remediation.attempt_id)
            .where(
                Remediation.section_id == section.id,
                QuizAttempt.learning_run_id == learning_run.id,
            )
            .order_by(Remediation.created_at)
        ).all()
        latest_remediation_by_attempt = {
            item.attempt_id: item for item in remediation_revisions
        }
        remediations = list(latest_remediation_by_attempt.values())
        if binding and remediations:
            bound_remediation = next(
                (
                    item
                    for item in reversed(remediations)
                    if (
                        replacement := self.db.get(
                            QuizSet, item.replacement_quiz_id
                        )
                    )
                    and replacement.learning_contract_version_id
                    == binding.learning_contract_version_id
                ),
                None,
            )
            if bound_remediation:
                quiz = self.db.get(
                    QuizSet, bound_remediation.replacement_quiz_id
                )
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
        boundary_snapshot = (
            self.db.scalar(
                select(GovernanceDecisionSnapshot)
                .where(
                    GovernanceDecisionSnapshot.content_version_id == content.id,
                    GovernanceDecisionSnapshot.decision_scope
                    == "content_publication",
                    GovernanceDecisionSnapshot.quiz_set_id.is_(None),
                )
                .order_by(GovernanceDecisionSnapshot.created_at.desc())
            )
            if content
            else None
        )
        origin_run = (
            self.db.get(GenerationRun, content.generation_run_id)
            if content and content.generation_run_id
            else None
        )
        labeling_metadata = (
            load(content.labeling_metadata_json, {}) if content else {}
        ) or {}
        boundary_validation_passed = bool(
            content
            and content.schema_version != "legacy"
            and content.prompt_version != "legacy"
            and origin_run
            and origin_run.status == "succeeded"
            and origin_run.id == content.generation_run_id
            and origin_run.schema_version == content.schema_version
            and origin_run.prompt_version == content.prompt_version
            and boundary_snapshot
            and boundary_snapshot.allowed
            and boundary_snapshot.assessment_eligible
            and boundary_snapshot.mode == "contract_boundary"
            and boundary_snapshot.learning_contract_version_id
            == content.learning_contract_version_id
            and boundary_snapshot.actor_kind == "generation_attempt"
            and boundary_snapshot.actor_id == origin_run.id
            and labeling_metadata.get("schemaVersionOfCandidate")
            == content.schema_version
            and labeling_metadata.get("promptVersion") == content.prompt_version
            and labeling_metadata.get("ruleVersion")
            == boundary_snapshot.rule_version
        )
        boundary_validation = {
            "status": (
                "passed"
                if boundary_validation_passed
                else "legacy"
                if content and content.schema_version == "legacy"
                else "unverified"
            ),
            "ruleVersion": (
                boundary_snapshot.rule_version if boundary_snapshot else None
            ),
        }
        questions = load(quiz.questions_json, []) if quiz else []
        def public_questions(items):
            return [
                {
                    **{
                        key: value
                        for key, value in question.items()
                        if key not in {
                            "correct",
                            "explanation",
                            "claim_block_indexes",
                        }
                    },
                    "selectionMode": (
                        "multiple"
                        if len(set(question.get("correct", []))) > 1
                        else "single"
                    ),
                }
                for question in items
            ]

        public = public_questions(questions)
        governance = governance_view_for_quiz(
            self.db,
            quiz.id if quiz else None,
        )
        latest_attempt = (
            self.db.scalar(
                select(QuizAttempt)
                .where(
                    QuizAttempt.learning_run_id == learning_run.id,
                    QuizAttempt.user_id == self.user_id,
                    QuizAttempt.quiz_set_id == quiz.id,
                )
                .order_by(QuizAttempt.created_at.desc())
            )
            if quiz
            else None
        )
        latest_attempt_results = (
            load(latest_attempt.results_json, []) if latest_attempt else []
        )
        latest_attempt_quiz = (
            self.db.get(QuizSet, latest_attempt.quiz_set_id)
            if latest_attempt
            else None
        )
        latest_attempt_tasks = (
            [
                task_view(task)
                for task in workflow_tasks
                if task.trigger_id == latest_attempt.id
            ]
            if latest_attempt
            else []
        )
        return {
            **self._section_summary(section),
            "versionBinding": (
                {
                    "id": binding.id,
                    "learningContractVersionId": (
                        binding.learning_contract_version_id
                    ),
                    "contentVersionId": binding.content_version_id,
                    "initialQuizSetId": binding.initial_quiz_set_id,
                    "firstReadAt": timestamp(binding.first_read_at),
                    "source": binding.source,
                }
                if binding
                else None
            ),
            "generation": self._generation(run) if run else None,
            "content": {
                "id": content.id,
                "version": content.version,
                "blocks": load(content.blocks_json, []),
                "sources": load(content.sources_json, []),
                "sourceVerification": load(verification.report_json, []) if verification else [],
                "confidence": content.confidence,
                "publicationStatus": content.publication_status,
                "generationMode": content.generation_mode,
                "rightsStatus": content.rights_status,
                "factualStatus": content.factual_status,
                "aiGenerated": content.ai_generated,
                "schemaVersion": content.schema_version,
                "promptVersion": content.prompt_version,
                "boundaryValidation": boundary_validation,
            }
            if content
            else None,
            "quiz": {
                "id": quiz.id,
                "generation": quiz.generation,
                "publicationStatus": quiz.publication_status,
                "questions": public,
                "governance": governance,
            } if quiz else None,
            "latestAttemptReview": (
                {
                    "attemptId": latest_attempt.id,
                    "score": sum(
                        bool(item.get("correct"))
                        for item in latest_attempt_results
                    ),
                    "total": len(latest_attempt_results),
                    "passed": latest_attempt.passed,
                    "perfect": bool(latest_attempt_results) and all(
                        item.get("correct") for item in latest_attempt_results
                    ),
                    "results": latest_attempt_results,
                    "questions": public_questions(
                        load(latest_attempt_quiz.questions_json, [])
                    ) if latest_attempt_quiz else [],
                    "remediation": None,
                    "nextQuiz": None,
                    "workflowTasks": latest_attempt_tasks,
                    "noteGeneration": None,
                }
                if latest_attempt
                else None
            ),
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
        started = (
            run.started_at
            if run.started_at.tzinfo
            else run.started_at.replace(tzinfo=timezone.utc)
        )
        finished = run.finished_at or now()
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
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
            "durationMs": max(
                0,
                int((finished - started).total_seconds() * 1000),
            ),
        }

    async def submit_quiz(self, section_id, body, idempotency_key=None):
        return await SubmitQuiz(
            self.db,
            user_id=self.user_id,
        ).execute(section_id, body, idempotency_key)

    def reassess_quiz_attempt(self, section_id, attempt_id):
        return SubmitQuiz(
            self.db,
            user_id=self.user_id,
        ).reassess(section_id, attempt_id)

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
            if task.task_type == "content_feedback_regeneration":
                result = await self._regenerate_from_content_feedback(
                    task,
                    payload,
                )
            elif task.task_type == "initial_book_preload":
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
                    .order_by(Remediation.created_at.desc())
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

    async def _regenerate_from_content_feedback(
        self,
        task: LearningTask,
        payload: dict,
    ) -> dict:
        feedback_id = payload.get("feedbackId")
        feedback = self.db.scalar(
            select(UserFeedback).where(
                UserFeedback.id == feedback_id,
                UserFeedback.user_id == task.user_id,
                UserFeedback.scope == "content_block",
                UserFeedback.section_id == task.section_id,
                UserFeedback.content_version_id
                == payload.get("contentVersionId"),
                UserFeedback.block_id == payload.get("blockId"),
            )
        )
        if not feedback or task.trigger_id != feedback.id:
            raise AppError(
                "反馈重生成任务缺少可信的反馈事实",
                code="CONTENT_FEEDBACK_FACT_MISSING",
                status=409,
            )
        prior_runs = self.db.scalars(
            select(GenerationRun)
            .where(
                GenerationRun.section_id == task.section_id,
                GenerationRun.operation == "regeneration",
                GenerationRun.status == "succeeded",
            )
            .order_by(GenerationRun.started_at.desc())
            .limit(20)
        ).all()
        completed = next(
            (
                load(item.trace_json, {})
                for item in prior_runs
                if load(item.trace_json, {}).get("feedbackId") == feedback.id
            ),
            None,
        )
        if completed:
            return {
                "contentVersionId": completed.get("contentVersionId"),
                "quizSetId": completed.get("quizSetId"),
                "feedbackId": feedback.id,
            }
        content = self.db.get(ContentVersion, feedback.content_version_id)
        if not content or content.section_id != task.section_id:
            raise AppError(
                "反馈对应的正文版本不存在",
                code="FEEDBACK_TARGET_NOT_FOUND",
                status=404,
            )
        block = next(
            (
                item
                for item in load(content.blocks_json, [])
                if item.get("id") == feedback.block_id
            ),
            None,
        )
        if not block:
            raise AppError(
                "反馈对应的段落不存在",
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
        if block_snapshot_hash != feedback.block_snapshot_hash:
            raise AppError(
                "反馈段落快照与原始事实不一致",
                code="FEEDBACK_BLOCK_SNAPSHOT_MISMATCH",
                status=409,
            )
        instructions = {
            "inaccurate": "核查并纠正这一段的不准确内容",
            "unclear": "重新解释这一段，使机制与因果关系更清楚",
            "poor_example": "更换或改进这一段的例子",
            "typo": "修正这一段的文字错误并检查相邻表述",
            "layout": "改进这一段及相关内容块的表达结构与呈现形式",
            "other": "依据补充说明改进这一段",
        }
        feedback_context = {
            "feedbackId": feedback.id,
            "feedbackType": feedback.feedback_type,
            "instruction": instructions.get(
                feedback.feedback_type,
                "依据反馈改进这一段",
            ),
            "message": feedback.message,
            "contentVersionId": feedback.content_version_id,
            "blockId": feedback.block_id,
            "blockSnapshotHash": feedback.block_snapshot_hash,
            "blockSnapshot": block,
        }
        view = await self.generate_section(
            task.section_id,
            regenerate=True,
            regeneration_feedback=feedback_context,
        )
        generated_content = view.get("content") or {}
        generated_quiz = view.get("quiz") or {}
        if (
            not generated_content.get("id")
            or generated_content.get("id") == feedback.content_version_id
            or not generated_quiz.get("id")
        ):
            raise AppError(
                "反馈重生成完成但没有得到新的正文与测验版本",
                code="CONTENT_FEEDBACK_REGENERATION_RESULT_MISSING",
                status=500,
            )
        return {
            "contentVersionId": generated_content["id"],
            "quizSetId": generated_quiz["id"],
            "feedbackId": feedback.id,
        }

    async def _preload_initial_book(self, chapter_id):
        if not chapter_id:
            raise AppError(
                "首节预生成任务缺少章节",
                code="INITIAL_CHAPTER_MISSING",
                status=500,
            )
        await self.generate_chapter(
            chapter_id,
            first_section_status="preparing",
        )
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
        target_progress = self.progress.for_section(target)
        if target_progress.status != "preparing":
            self.progress.set_status(target_progress, "preparing")
            self.db.commit()
        await self.generate_section(target.id)
        self.progress.set_status(target_progress, "available")
        self.db.commit()
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
                await self.generate_chapter(
                    next_chapter.id,
                    first_section_status="preparing",
                )
                target = self.db.scalar(
                    select(Section)
                    .where(Section.chapter_id == next_chapter.id)
                    .order_by(Section.position)
                )
        if not target:
            return {"targetSectionId": None, "endOfSeries": True}
        target_progress = self.progress.for_section(target)
        if target_progress.status != "preparing":
            self.progress.set_status(target_progress, "preparing")
            self.db.commit()
        await self.generate_section(target.id)
        self.progress.set_status(target_progress, "available")
        self.db.commit()
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

    def _memory(self, shelf_id, limit=30, *, include_legacy=False):
        target_scope = (
            select(SectionAssessmentTarget.assessment_target_id)
            .join(Section, Section.id == SectionAssessmentTarget.section_id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .where(
                Book.shelf_id == shelf_id,
                Book.deleted_at.is_(None),
            )
        )
        projection_rows = self.db.execute(
            select(KnowledgeStateProjection, AssessmentTarget)
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == KnowledgeStateProjection.assessment_target_id,
            )
            .where(
                KnowledgeStateProjection.user_id == self.user_id,
                KnowledgeStateProjection.assessment_target_id.in_(target_scope),
            )
            .order_by(KnowledgeStateProjection.updated_at.desc())
            .limit(limit)
        ).all()
        target_ids = [target.id for _, target in projection_rows]
        evidence_counts = dict(
            self.db.execute(
                select(
                    AssessmentObservation.assessment_target_id,
                    func.count(AssessmentObservation.id),
                )
                .where(
                    AssessmentObservation.user_id == self.user_id,
                    AssessmentObservation.assessment_target_id.in_(target_ids),
                )
                .group_by(AssessmentObservation.assessment_target_id)
            ).all()
        ) if target_ids else {}
        result = [
            {
                "concept": target.objective_statement,
                "mastery": round(state.p_known_ppm / 10_000),
                "evidenceCount": evidence_counts.get(target.id, 0),
                "summary": (
                    f"BKT 掌握概率 {state.p_known_ppm / 10_000:.1f}%；"
                    f"声明 {state.claim_status}；保持轮次 {state.retention_rounds}"
                ),
                "assessmentTargetId": target.id,
                "pKnown": round(state.p_known_ppm / 1_000_000, 6),
                "uncertainty": round(state.uncertainty_ppm / 1_000_000, 6),
                "claimStatus": state.claim_status,
                "retentionRounds": state.retention_rounds,
                "parameterSetVersion": state.parameter_set_version,
                "projectionRuleVersion": state.projection_rule_version,
                "sourceObservationWatermark": state.source_observation_watermark,
            }
            for state, target in projection_rows
        ]

        # Ask Me and pre-M2 evidence still use the legacy memory projection.
        # Keep it as a compatibility fallback, but never let it override a BKT
        # projection for the same measured objective.
        projected_concepts = {
            " ".join(item["concept"].strip().casefold().split())
            for item in result
        }
        if include_legacy and len(result) < limit:
            legacy_rows = self.db.scalars(
                select(LearningMemory)
                .where(
                    LearningMemory.user_id == self.user_id,
                    LearningMemory.shelf_id == shelf_id,
                )
                .order_by(LearningMemory.updated_at.desc())
                .limit(limit)
            ).all()
            for item in legacy_rows:
                key = " ".join(item.concept.strip().casefold().split())
                if key in projected_concepts:
                    continue
                result.append({
                    "concept": item.concept,
                    "mastery": item.mastery_score,
                    "evidenceCount": item.evidence_count,
                    "summary": item.summary,
                    "projectionRuleVersion": "legacy_linear_v1",
                })
                if len(result) == limit:
                    break
        return result

    def learning_memory(self, shelf_id=None):
        if shelf_id:
            self.shelf(shelf_id)
            return self._memory(shelf_id, 200, include_legacy=True)
        shelves = self.db.scalars(select(Shelf).where(Shelf.user_id == self.user_id)).all()
        return {
            item.id: self._memory(item.id, 200, include_legacy=True)
            for item in shelves
        }

    def due_reviews(self, daily_budget=10):
        return ReviewAssignmentService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
            context_builder=self.generation_contexts,
            context_resolver=self.contexts,
            memory_loader=self._memory,
        ).due(daily_budget=daily_budget)

    async def start_review(self, assignment_id: str):
        return await ReviewAssignmentService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
            context_builder=self.generation_contexts,
            context_resolver=self.contexts,
            memory_loader=self._memory,
        ).start(assignment_id)

    def submit_review(self, assignment_id: str, body, idempotency_key=None):
        return ReviewAssignmentService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
            context_builder=self.generation_contexts,
            context_resolver=self.contexts,
            memory_loader=self._memory,
        ).submit(
            assignment_id,
            body.answers,
            idempotency_key=idempotency_key,
        )

    def skip_review(self, assignment_id: str):
        return ReviewAssignmentService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
            context_builder=self.generation_contexts,
            context_resolver=self.contexts,
            memory_loader=self._memory,
        ).skip(assignment_id)

    def expire_review(self, assignment_id: str):
        return ReviewAssignmentService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
            context_builder=self.generation_contexts,
            context_resolver=self.contexts,
            memory_loader=self._memory,
        ).expire(assignment_id)

    async def _ensure_note(self, section):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section.id,
        )
        learning_run = self.progress.active_run(context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        contract = (
            self.db.get(
                LearningContractVersion,
                binding.learning_contract_version_id,
            )
            if binding
            else None
        )
        mission = (
            self.db.get(LearningMissionVersion, contract.mission_version_id)
            if contract
            else None
        )
        context_pack = self.generation_contexts.build(
            "learning_note",
            shelf=context.shelf,
            series=context.series,
            book=context.book,
            chapter=context.chapter,
            section=section,
            mission=mission,
            contract=contract,
            memory=self._memory(context.book.shelf_id, 30),
        )
        await GenerateLearningNote(
            self.db,
            user_id=self.user_id,
            learning_run_id=learning_run.id,
            tutor=self.ai,
            section_reader=self.section,
            generation_context=context_pack.payload(),
        ).execute(section)

    def _note(self, note):
        summaries = self.db.scalars(
            select(LearningNoteSummary)
            .where(LearningNoteSummary.note_id == note.id)
            .order_by(LearningNoteSummary.version)
        ).all()
        supplements = self.db.scalars(
            select(LearningNoteReviewSupplement)
            .where(LearningNoteReviewSupplement.note_id == note.id)
            .order_by(
                LearningNoteReviewSupplement.created_at,
                LearningNoteReviewSupplement.id,
            )
        ).all()
        user_revisions = self.db.scalars(
            select(LearningNoteUserRevision)
            .where(LearningNoteUserRevision.note_id == note.id)
            .order_by(LearningNoteUserRevision.version)
        ).all()
        verification_rows = self.db.execute(
            select(KnowledgeStateProjection, AssessmentTarget)
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == KnowledgeStateProjection.assessment_target_id,
            )
            .join(
                SectionAssessmentTarget,
                SectionAssessmentTarget.assessment_target_id
                == AssessmentTarget.id,
            )
            .where(
                KnowledgeStateProjection.user_id == self.user_id,
                SectionAssessmentTarget.section_id == note.section_id,
            )
            .order_by(AssessmentTarget.created_at)
        ).all()
        latest_summary = summaries[-1] if summaries else None
        latest_user = user_revisions[-1] if user_revisions else None
        return {
            "id": note.id,
            "aiContent": load(
                latest_summary.content_json if latest_summary else note.ai_content_json,
                {},
            ),
            "userContent": load(
                latest_user.content_json if latest_user else note.user_content_json,
                {},
            ),
            "version": note.version,
            "layers": {
                "learningSummary": (
                    {
                        "version": latest_summary.version,
                        "content": load(latest_summary.content_json, {}),
                        "sourceContentVersionId": (
                            latest_summary.source_content_version_id
                        ),
                        "sourceContractVersion": (
                            latest_summary.source_contract_version
                        ),
                        "sourceObservationWatermark": (
                            latest_summary.source_observation_watermark
                        ),
                        "generationRuleVersion": (
                            latest_summary.generation_rule_version
                        ),
                        "createdAt": timestamp(latest_summary.created_at),
                    }
                    if latest_summary
                    else None
                ),
                "reviewSupplements": [
                    {
                        "id": item.id,
                        "reviewEpisodeId": item.review_episode_id,
                        "content": load(item.content_json, {}),
                        "authorKind": item.author_kind,
                        "sourceObservationWatermark": (
                            item.source_observation_watermark
                        ),
                        "createdAt": timestamp(item.created_at),
                    }
                    for item in supplements
                ],
                "userRevision": (
                    {
                        "version": latest_user.version,
                        "content": load(latest_user.content_json, {}),
                        "basedOnSummaryVersion": (
                            latest_user.based_on_summary_version
                        ),
                        "source": latest_user.source,
                        "createdAt": timestamp(latest_user.created_at),
                    }
                    if latest_user
                    else None
                ),
            },
            "verificationAnnotations": [
                {
                    "assessmentTargetId": target.id,
                    "objective": target.objective_statement,
                    "dimension": target.dimension,
                    "pKnown": round(state.p_known_ppm / 1_000_000, 6),
                    "uncertainty": round(
                        state.uncertainty_ppm / 1_000_000,
                        6,
                    ),
                    "claimStatus": state.claim_status,
                    "retentionRounds": state.retention_rounds,
                    "parameterSetVersion": state.parameter_set_version,
                    "projectionRuleVersion": state.projection_rule_version,
                    "sourceObservationWatermark": (
                        state.source_observation_watermark
                    ),
                }
                for state, target in verification_rows
            ],
        }

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
        latest_version = self.db.scalar(
            select(func.max(LearningNoteUserRevision.version)).where(
                LearningNoteUserRevision.note_id == note.id
            )
        ) or 0
        summary_version = self.db.scalar(
            select(func.max(LearningNoteSummary.version)).where(
                LearningNoteSummary.note_id == note.id
            )
        ) or 1
        self.db.add(
            LearningNoteUserRevision(
                id=uid("note_user_revision"),
                note_id=note.id,
                version=latest_version + 1,
                content_json=dump(content),
                based_on_summary_version=summary_version,
                source="user_edit",
            )
        )
        note.user_content_json, note.version, note.updated_at = (
            dump(content),
            note.version + 1,
            now(),
        )
        self.db.commit()
        return self._note(note)

    def add_note_review_supplement(
        self,
        section_id: str,
        review_episode_id: str,
        content: dict,
    ):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
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
        existing = self.db.scalar(
            select(LearningNoteReviewSupplement).where(
                LearningNoteReviewSupplement.note_id == note.id,
                LearningNoteReviewSupplement.review_episode_id
                == review_episode_id,
            )
        )
        if existing:
            if load(existing.content_json, {}) != content:
                raise AppError(
                    "该复习轮次已经形成了不同的笔记补充",
                    code="NOTE_REVIEW_EPISODE_REUSED",
                    status=409,
                )
            return self._note(note)
        watermark = self.db.scalar(
            select(func.max(AssessmentObservation.sequence)).where(
                AssessmentObservation.learning_run_id == learning_run.id,
                AssessmentObservation.user_id == self.user_id,
                AssessmentObservation.section_id == section_id,
                AssessmentObservation.learning_episode_id == review_episode_id,
                AssessmentObservation.assistance_mode == "unassisted_review",
                AssessmentObservation.qualification_at_creation.in_(
                    ("eligible", "eligible_grouped")
                ),
            )
        )
        if not watermark:
            raise AppError(
                "复习补充必须绑定一次已完成的无辅助复习",
                code="NOTE_REVIEW_EPISODE_INVALID",
                status=409,
            )
        self.db.add(
            LearningNoteReviewSupplement(
                id=uid("note_review_supplement"),
                note_id=note.id,
                review_episode_id=review_episode_id,
                content_json=dump(content),
                author_kind="user",
                source_observation_watermark=watermark,
            )
        )
        note.version += 1
        note.updated_at = now()
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            concurrent = self.db.scalar(
                select(LearningNoteReviewSupplement).where(
                    LearningNoteReviewSupplement.note_id == note.id,
                    LearningNoteReviewSupplement.review_episode_id
                    == review_episode_id,
                )
            )
            if not concurrent:
                raise
            if load(concurrent.content_json, {}) != content:
                raise AppError(
                    "该复习轮次已经形成了不同的笔记补充",
                    code="NOTE_REVIEW_EPISODE_REUSED",
                    status=409,
                )
        return self._note(note)

    def prepare_ask(self, section_id, body):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(section_context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )
        if not binding:
            mission = self.missions.current_version(section_context.series.id)
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=section_context.section,
                mission_version_id=mission.id,
                source="ask_ai_block_recovery",
                uid=uid,
                preferred_block_id=body.block_id,
            )
            self.db.commit()
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
                learning_contract_version_id=(
                    binding.learning_contract_version_id
                ),
                content_version_id=binding.content_version_id,
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
        contract = self.db.get(
            LearningContractVersion,
            binding.learning_contract_version_id,
        )
        mission = (
            self.db.get(LearningMissionVersion, contract.mission_version_id)
            if contract
            else None
        )
        cross_section_memory = self._memory(
            section_context.book.shelf_id,
            10,
        )
        interaction = {
            "anchorBlockId": body.block_id,
            "question": body.question,
            "currentThreadFullHistory": current_history,
            "relatedThreadSummaries": related_summaries,
        }
        context_pack = self.generation_contexts.build(
            "ask_ai",
            shelf=section_context.shelf,
            series=section_context.series,
            book=section_context.book,
            chapter=section_context.chapter,
            section=section_context.section,
            mission=mission,
            contract=contract,
            memory=cross_section_memory,
            interaction=interaction,
        )
        return {
            "session": session,
            "suggestedThreadId": suggested,
            "request": self.generation_contexts.attach({
                "section": section_view,
                "anchorBlockId": body.block_id,
                "question": body.question,
                "requestedThreadId": body.thread_id,
                "forcedRelation": body.force_relation,
                "newThreadId": suggested,
                "weightedContext": {
                    "currentThreadFullHistory": current_history,
                    "relatedThreadSummaries": related_summaries,
                    "crossSectionMemory": cross_section_memory,
                },
            }, context_pack),
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

    async def feedback_repair_stream(self, feedback_id: str):
        feedback = self.db.scalar(
            select(UserFeedback).where(
                UserFeedback.id == feedback_id,
                UserFeedback.user_id == self.user_id,
                UserFeedback.scope == "content_block",
            )
        )
        if not feedback:
            raise AppError(
                "反馈不存在或不属于当前用户",
                code="FEEDBACK_NOT_FOUND",
                status=404,
            )
        content = self.db.get(ContentVersion, feedback.content_version_id)
        if not content or content.section_id != feedback.section_id:
            raise AppError(
                "反馈对应的正文版本不存在",
                code="FEEDBACK_TARGET_NOT_FOUND",
                status=404,
            )
        blocks = load(content.blocks_json, [])
        target_index = next(
            (
                index
                for index, item in enumerate(blocks)
                if item.get("id") == feedback.block_id
            ),
            None,
        )
        if target_index is None:
            raise AppError(
                "反馈对应的段落不存在",
                code="FEEDBACK_BLOCK_NOT_FOUND",
                status=404,
            )
        target_block = blocks[target_index]
        snapshot_hash = hashlib.sha256(
            json.dumps(
                target_block,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if snapshot_hash != feedback.block_snapshot_hash:
            raise AppError(
                "反馈段落与提交时的内容不一致",
                code="FEEDBACK_BLOCK_SNAPSHOT_MISMATCH",
                status=409,
            )

        # V2 corrections regenerate the complete lesson candidate and quiz as
        # one atomic version. The SSE endpoint remains for client compatibility,
        # but it streams the already-published replacement block instead of
        # persisting an independently generated block patch.
        if callable(getattr(self.ai, "generate_lesson", None)):
            def mapped_replacement_block(content_blocks, trace):
                mapping = trace.get("feedbackReplacement") or {}
                if mapping.get("sourceBlockId") != feedback.block_id:
                    return None
                replacement_block_id = mapping.get("replacementBlockId")
                return next(
                    (
                        item
                        for item in content_blocks
                        if item.get("id") == replacement_block_id
                    ),
                    None,
                )

            completed_runs = self.db.scalars(
                select(GenerationRun)
                .where(
                    GenerationRun.section_id == feedback.section_id,
                    GenerationRun.operation == "regeneration",
                    GenerationRun.status == "succeeded",
                )
                .order_by(GenerationRun.started_at.desc())
                .limit(20)
            ).all()
            completed_trace = next(
                (
                    load(item.trace_json, {})
                    for item in completed_runs
                    if load(item.trace_json, {}).get("feedbackId") == feedback.id
                ),
                None,
            )
            if completed_trace:
                replacement = self.db.get(
                    ContentVersion,
                    completed_trace.get("contentVersionId"),
                )
                replacement_blocks = load(
                    replacement.blocks_json if replacement else "[]",
                    [],
                )
                if replacement and replacement_blocks:
                    replacement_block = mapped_replacement_block(
                        replacement_blocks,
                        completed_trace,
                    )
                    if replacement_block:
                        yield {"type": "delta", "delta": replacement_block["content"]}
                        yield {
                            "type": "done",
                            "feedbackId": feedback.id,
                            "contentVersionId": replacement.id,
                            "contentVersion": replacement.version,
                            "contentBlockId": replacement_block["id"],
                            "replayed": True,
                        }
                        return
            instructions = {
                "inaccurate": "核查并纠正这一段的不准确内容",
                "unclear": "重新解释这一段，使机制与因果关系更清楚",
                "poor_example": "更换或改进这一段的例子",
                "typo": "修正这一段的文字错误并检查相邻表述",
                "layout": "改进这一段及相关内容块的表达结构与呈现形式",
                "other": "依据补充说明改进这一段",
            }
            view = await self.generate_section(
                feedback.section_id,
                regenerate=True,
                regeneration_feedback={
                    "feedbackId": feedback.id,
                    "feedbackType": feedback.feedback_type,
                    "instruction": instructions.get(
                        feedback.feedback_type,
                        "依据反馈改进这一段",
                    ),
                    "message": feedback.message,
                    "contentVersionId": feedback.content_version_id,
                    "blockId": feedback.block_id,
                    "blockSnapshotHash": feedback.block_snapshot_hash,
                    "blockSnapshot": target_block,
                },
            )
            replacement_content = view.get("content") or {}
            replacement_blocks = replacement_content.get("blocks") or []
            if not replacement_blocks:
                raise AppError(
                    "反馈重生成完成但没有得到新的已发布正文",
                    code="CONTENT_FEEDBACK_REGENERATION_RESULT_MISSING",
                    status=500,
                )
            generation_trace = (
                (view.get("generation") or {}).get("trace") or {}
            )
            replacement_block = mapped_replacement_block(
                replacement_blocks,
                generation_trace,
            )
            if not replacement_block:
                raise AppError(
                    "反馈重生成完成但缺少已校验的段落替换映射",
                    code="FEEDBACK_REPLACEMENT_MAPPING_MISSING",
                    status=500,
                )
            yield {"type": "delta", "delta": replacement_block["content"]}
            yield {
                "type": "done",
                "feedbackId": feedback.id,
                "contentVersionId": replacement_content["id"],
                "contentVersion": replacement_content["version"],
                "contentBlockId": replacement_block["id"],
                "replayed": False,
            }
            return

        completed_runs = self.db.scalars(
            select(GenerationRun)
            .where(
                GenerationRun.section_id == feedback.section_id,
                GenerationRun.operation == "feedback_repair",
                GenerationRun.status == "succeeded",
            )
            .order_by(GenerationRun.started_at.desc())
            .limit(20)
        ).all()
        for completed_run in completed_runs:
            completed_trace = load(completed_run.trace_json, {})
            if completed_trace.get("feedbackId") != feedback.id:
                continue
            repaired_content = self.db.get(
                ContentVersion,
                completed_trace.get("contentVersionId"),
            )
            repaired_block = next(
                (
                    item
                    for item in load(
                        repaired_content.blocks_json if repaired_content else "[]",
                        [],
                    )
                    if item.get("id") == completed_trace.get("contentBlockId")
                ),
                None,
            )
            if repaired_content and repaired_block:
                yield {"type": "delta", "delta": repaired_block["content"]}
                yield {
                    "type": "done",
                    "feedbackId": feedback.id,
                    "contentVersionId": repaired_content.id,
                    "contentVersion": repaired_content.version,
                    "contentBlockId": repaired_block["id"],
                    "replayed": True,
                }
                return

        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=feedback.section_id,
        )
        learning_run = self.progress.active_run(context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == feedback.section_id,
            )
        )
        if not binding or binding.content_version_id != content.id:
            raise AppError(
                "正文已经更新，请在当前版本重新提交反馈",
                code="FEEDBACK_CONTENT_VERSION_STALE",
                status=409,
            )

        resource_key = f"section:{feedback.section_id}"
        owner_id = acquire_generation_lease(self.db, resource_key)
        if owner_id is None:
            raise AppError(
                "这一节正在处理另一条补救请求",
                code="GENERATION_IN_PROGRESS",
                status=409,
                retryable=True,
            )
        attempt = (
            self.db.scalar(
                select(func.max(GenerationRun.attempt)).where(
                    GenerationRun.section_id == feedback.section_id
                )
            )
            or 0
        ) + 1
        run = GenerationRun(
            id=uid("generation"),
            section_id=feedback.section_id,
            operation="feedback_repair",
            attempt=attempt,
            status="running",
            model=getattr(self.ai, "model", ""),
            trace_json=dump(
                {
                    "feedbackId": feedback.id,
                    "contentVersionId": content.id,
                    "contentBlockId": feedback.block_id,
                    "delivery": "sse_passthrough_v1",
                }
            ),
        )
        self.db.add(run)
        self.db.commit()
        repair_request = {
            "section": {
                "id": context.section.id,
                "title": context.section.title,
                "question": context.section.question,
                "objectives": load(context.section.objectives_json, []),
            },
            "targetBlock": target_block,
            "previousBlock": blocks[target_index - 1] if target_index > 0 else None,
            "nextBlock": (
                blocks[target_index + 1]
                if target_index + 1 < len(blocks)
                else None
            ),
            "feedback": {
                "type": feedback.feedback_type,
                "message": feedback.message,
            },
            "sources": load(content.sources_json, []),
        }
        parts: list[str] = []
        try:
            stream_repair = getattr(self.ai, "repair_stream", None)
            if not callable(stream_repair):
                raise AiError(
                    "当前模型不支持流式补救",
                    code="AI_REPAIR_STREAM_UNSUPPORTED",
                )
            async for delta in stream_repair(repair_request):
                if delta:
                    parts.append(delta)
                    yield {"type": "delta", "delta": delta}
            repaired_text = "".join(parts)
            if not repaired_text.strip():
                raise AiError(
                    "模型没有返回补救内容",
                    code="AI_REPAIR_EMPTY_RESPONSE",
                )
            if not renew_generation_lease(self.db, resource_key, owner_id):
                raise AppError(
                    "补救请求已经失去写入租约",
                    code="GENERATION_LEASE_LOST",
                    status=409,
                )
            binding = self.db.get(LearningRunSectionBinding, binding.id)
            if not binding or binding.content_version_id != content.id:
                raise AppError(
                    "补救生成期间正文已经更新，当前结果未覆盖新版本",
                    code="SECTION_BINDING_STALE",
                    status=409,
                )
            next_version = (
                self.db.scalar(
                    select(func.max(ContentVersion.version)).where(
                        ContentVersion.section_id == content.section_id
                    )
                )
                or 0
            ) + 1
            repaired_content = ContentVersion(
                id=uid("content"),
                section_id=content.section_id,
                learning_contract_version_id=content.learning_contract_version_id,
                version=next_version,
                blocks_json="[]",
                sources_json=content.sources_json,
                confidence=content.confidence,
            )
            repaired_blocks = []
            repaired_block_id = ""
            for position, original_block in enumerate(blocks, 1):
                repaired_block = dict(original_block)
                repaired_block["id"] = f"block_{repaired_content.id}_{position}"
                repaired_block["version"] = next_version
                if position - 1 == target_index:
                    repaired_block["kind"] = "text"
                    repaired_block["content"] = repaired_text
                    repaired_block_id = repaired_block["id"]
                repaired_blocks.append(repaired_block)
            repaired_content.blocks_json = dump(repaired_blocks)
            self.db.add(repaired_content)
            self.db.flush()
            audit = load(binding.lineage_audit_json, {})
            repair_history = list(audit.get("feedbackRepairs") or [])
            repair_history.append(
                {
                    "generationRunId": run.id,
                    "feedbackId": feedback.id,
                    "fromContentVersionId": content.id,
                    "toContentVersionId": repaired_content.id,
                    "fromBlockId": feedback.block_id,
                    "toBlockId": repaired_block_id,
                    "delivery": "sse_passthrough_v1",
                    "changedAt": timestamp(now()),
                }
            )
            binding.content_version_id = repaired_content.id
            binding.source = "feedback_stream_repair"
            binding.source_fact_id = feedback.id
            binding.lineage_audit_json = dump(
                {
                    **audit,
                    "contentVersionId": repaired_content.id,
                    "feedbackRepairs": repair_history,
                }
            )
            resume = self.db.scalar(
                select(LearningResumePosition).where(
                    LearningResumePosition.user_id == self.user_id,
                    LearningResumePosition.learning_run_id == learning_run.id,
                    LearningResumePosition.section_id == feedback.section_id,
                )
            )
            if resume:
                resume.content_version_id = repaired_content.id
                if resume.block_id == feedback.block_id:
                    resume.block_id = repaired_block_id
                resume.updated_at = now()
            finished_at = now()
            run.status = "succeeded"
            run.finished_at = finished_at
            run.trace_json = dump(
                {
                    "feedbackId": feedback.id,
                    "supersedesContentVersionId": content.id,
                    "contentVersionId": repaired_content.id,
                    "contentBlockId": repaired_block_id,
                    "delivery": "sse_passthrough_v1",
                    "finishedAt": timestamp(finished_at),
                }
            )
            self.db.commit()
            yield {
                "type": "done",
                "feedbackId": feedback.id,
                "contentVersionId": repaired_content.id,
                "contentVersion": repaired_content.version,
                "contentBlockId": repaired_block_id,
                "replayed": False,
            }
        except BaseException as error:
            self.db.rollback()
            failed_run = self.db.get(GenerationRun, run.id)
            if failed_run:
                failed_run.status = "failed"
                failed_run.error_code = safe_error_code(error)
                failed_run.error_message = (
                    str(error)[:500]
                    if isinstance(error, AppError)
                    else "补救内容生成失败，请重试"
                )
                failed_run.finished_at = now()
                self.db.commit()
            raise
        finally:
            release_generation_lease(self.db, resource_key, owner_id)

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
            raise AppError("小节满分后才解锁深入讨论", code="ASK_ME_LOCKED", status=403)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        if not binding:
            mission = self.missions.current_version(section_context.series.id)
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=section,
                mission_version_id=mission.id,
                source="ask_me_start_recovery",
                uid=uid,
            )
            self.db.commit()
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
                raise AppError("请先开始深入讨论再作答", code="ASK_ME_NOT_STARTED")
            section_view = self.section(section_id)
            contract = self.db.get(
                LearningContractVersion,
                binding.learning_contract_version_id,
            )
            mission = (
                self.db.get(LearningMissionVersion, contract.mission_version_id)
                if contract
                else None
            )
            context_pack = self.generation_contexts.build(
                "ask_me",
                shelf=section_context.shelf,
                series=section_context.series,
                book=section_context.book,
                chapter=section_context.chapter,
                section=section,
                mission=mission,
                contract=contract,
                memory=self._memory(section_context.book.shelf_id, 10),
                interaction={"dimension": "mechanism", "priorRounds": []},
            )
            self.db.commit()
            turn = None
            for validation_attempt in range(1, 4):
                turn = await self.ai.ask_me(
                    self.generation_contexts.attach(
                        {
                            "section": section_view,
                            "dimension": "mechanism",
                            "previousAnswer": None,
                            "finalize": False,
                            "validationAttempt": validation_attempt,
                            "requiredEvaluation": "not_evaluated",
                        },
                        context_pack,
                    )
                )
                if turn.dimension == "mechanism" and turn.evaluation == "not_evaluated":
                    break
            if turn is None or turn.dimension != "mechanism" or turn.evaluation != "not_evaluated":
                raise AiError("Ask Me 首轮结构无效")
            session = AskMeSession(
                id=uid("askme"),
                learning_run_id=learning_run.id,
                section_id=section.id,
                user_id=self.user_id,
                learning_contract_version_id=(
                    binding.learning_contract_version_id
                ),
                content_version_id=binding.content_version_id,
                round_index=0,
                entries_json=dump([{
                    "dimension": "mechanism",
                    "prompt": turn.prompt,
                    "answer": None,
                    "evaluation": "not_evaluated",
                    "rationale": "",
                }]),
            )
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
        contract = self.db.get(
            LearningContractVersion,
            binding.learning_contract_version_id,
        )
        mission = (
            self.db.get(LearningMissionVersion, contract.mission_version_id)
            if contract
            else None
        )
        context_pack = self.generation_contexts.build(
            "ask_me",
            shelf=section_context.shelf,
            series=section_context.series,
            book=section_context.book,
            chapter=section_context.chapter,
            section=section,
            mission=mission,
            contract=contract,
            memory=self._memory(section_context.book.shelf_id, 10),
            interaction={
                "dimension": requested_dimension,
                "evaluatesDimension": current_dimension,
                "previousPrompt": entries[current]["prompt"],
                "previousAnswer": answer,
                "priorRounds": entries,
            },
        )
        self.db.commit()
        turn = None
        for validation_attempt in range(1, 4):
            turn = await self.ai.ask_me(
                self.generation_contexts.attach({
                    "section": section_view,
                    "dimension": requested_dimension,
                    "evaluatesDimension": current_dimension,
                    "previousPrompt": entries[current]["prompt"],
                    "previousAnswer": answer,
                    "priorRounds": entries,
                    "finalize": finalize,
                    "validationAttempt": validation_attempt,
                    "requiredEvaluation": ["strong", "partial", "weak"],
                }, context_pack)
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
