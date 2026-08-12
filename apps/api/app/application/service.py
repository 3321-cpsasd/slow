from asyncio import CancelledError, sleep
import hashlib
import json
from uuid import uuid4

from sqlalchemy import event, func, select
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
    AssessmentObservation,
    AssessmentTarget,
    Book,
    BookCapstone,
    Chapter,
    ChapterPractice,
    ContentVersion,
    GenerationRun,
    LearningEvidence,
    LearningMemory,
    LearningNote,
    LearningRun,
    LearningRunSectionBinding,
    LearningTask,
    LearningResumePosition,
    KnowledgeStateProjection,
    QuizAttempt,
    Remediation,
    Section,
    SectionAssessmentTarget,
    Shelf,
    User,
    UserFeedback,
    now,
)
from ..modules.library.context import ActiveLearningContextResolver
from ..modules.library.commands import CatalogCommandService
from ..modules.curriculum.chapter_planning import ChapterPlanningService
from ..modules.curriculum.book_planning import BookPlanningService
from ..modules.curriculum.baselines import CurriculumBaselineService
from ..modules.curriculum.series_planning import SeriesPlanningService
from ..modules.curriculum.policy import CHAPTER_SECTION_POLICY
from ..modules.artifacts.progress import ArtifactProgressStore
from ..modules.artifacts.service import ArtifactService
from ..modules.learning.commands import SubmitQuiz
from ..modules.learning.assessment import record_ask_me_assessment_facts
from ..modules.learning.generation_leases import (
    acquire_generation_lease,
    release_generation_lease,
    renew_generation_lease,
)
from ..modules.learning.milestones import MilestoneService
from ..modules.learning.missions import MissionService
from ..modules.learning.knowledge_map import KnowledgeMapService
from ..modules.learning.knowledge_ranks import knowledge_node_views_for_targets
from ..modules.learning.reviews import ReviewAssignmentService
from ..modules.learning.reinforcements import ReinforcementService
from ..modules.learning.contracts import (
    open_run_section,
)
from ..modules.learning.progress import ProgressStore
from ..modules.learning.daily_mode import DailyModeService
from ..modules.learning.tasks import (
    complete_task,
    fail_task,
    release_task,
    reset_failed_task,
    task_view,
)
from ..modules.tutoring.notes import LearningNoteService
from ..modules.tutoring.ask_me import AskMeService
from ..modules.tutoring.qa import QaService
from ..read_models.library import LibraryReadModel
from ..read_models.section import SectionReadModel

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
        self.daily_modes = DailyModeService(db, user_id=self.user_id)
        self.artifacts = ArtifactProgressStore(db, user_id=self.user_id)
        self.artifact_service = ArtifactService(
            db,
            user_id=self.user_id,
            contexts=self.contexts,
            progress=self.progress,
            artifact_progress=self.artifacts,
            attachment_storage=attachment_storage,
        )
        self.library_reads = LibraryReadModel(db, user_id=self.user_id)
        self.milestones = MilestoneService(db, user_id=self.user_id, uid=uid)
        self.missions = MissionService(db, user_id=self.user_id, uid=uid)
        self.curriculum_baselines = CurriculumBaselineService(db)
        self.generation_contexts = GenerationContextBuilder(
            db,
            user_id=self.user_id,
        )
        self.notes = LearningNoteService(
            db,
            user_id=self.user_id,
            tutor=self.ai,
            contexts=self.contexts,
            progress=self.progress,
            generation_contexts=self.generation_contexts,
            memory_loader=self._memory,
            section_reader=self.section,
            uid=uid,
            dump=dump,
            load=load,
            timestamp=timestamp,
        )
        self.ask_me_service = AskMeService(
            db,
            user_id=self.user_id,
            tutor=self.ai,
            contexts=self.contexts,
            progress=self.progress,
            missions=self.missions,
            generation_contexts=self.generation_contexts,
            section_reader=self.section,
            memory_loader=self._memory,
            evidence_recorder=self._add_evidence,
            evidence_context=self._context,
            uid=uid,
            dump=dump,
            load=load,
        )
        self.qa_service = QaService(
            db,
            user_id=self.user_id,
            tutor=self.ai,
            contexts=self.contexts,
            progress=self.progress,
            missions=self.missions,
            generation_contexts=self.generation_contexts,
            section_reader=self.section,
            memory_loader=self._memory,
            daily_mode_reader=self.daily_modes.current,
            uid=uid,
            dump=dump,
            load=load,
        )
        self.chapter_planning = ChapterPlanningService(
            db,
            ai,
            user_id=self.user_id,
            contexts=self.contexts,
            progress=self.progress,
            artifacts=self.artifacts,
            missions=self.missions,
            generation_contexts=self.generation_contexts,
            memory_provider=self._memory,
            chapter_view=self._chapter,
        )
        self.book_planning = BookPlanningService(
            db,
            ai,
            user_id=self.user_id,
            contexts=self.contexts,
            progress=self.progress,
            missions=self.missions,
            milestones=self.milestones,
            generation_contexts=self.generation_contexts,
            memory_provider=self._memory,
            book_view=self.book,
        )
        self.series_planning = SeriesPlanningService(
            db,
            ai,
            user_id=self.user_id,
            progress=self.progress,
            artifacts=self.artifacts,
            missions=self.missions,
            milestones=self.milestones,
            baselines=self.curriculum_baselines,
            generation_contexts=self.generation_contexts,
            shelf_provider=self.shelf,
            memory_provider=self._memory,
            series_view=self.series,
        )
        self.section_reads = SectionReadModel(
            db,
            user_id=self.user_id,
            contexts=self.contexts,
            progress=self.progress,
            note_reader=self.notes.view,
        )
        self.catalog_commands = CatalogCommandService(
            db,
            user_id=self.user_id,
            contexts=self.contexts,
            progress=self.progress,
            shelf_view=self._shelf,
            book_view=self.book,
            chapter_view=self._chapter,
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
        view["dailyMode"] = self.daily_modes.current()
        view["milestoneDashboard"] = self.milestones.dashboard(
            library=view,
            profile=profile,
            resume=resume,
        )
        return view

    def daily_mode(self):
        return self.daily_modes.current()

    def update_daily_mode(self, body, idempotency_key: str):
        return self.daily_modes.activate(body, idempotency_key)

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
        return self.catalog_commands.create_shelf(body)

    async def create_plan(self, body, idempotency_key: str | None = None):
        return await self.series_planning.create(body, idempotency_key)

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
        return self.catalog_commands.delete_series(series_id)

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
        return self.catalog_commands.delete_book(book_id)

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
            "workloadHint": (
                CHAPTER_SECTION_POLICY.workload(len(sections))
                if sections
                else None
            ),
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

    def add_chapter(self, book_id, body):
        return self.catalog_commands.add_chapter(book_id, body)

    def update_chapter(self, chapter_id, body):
        return self.catalog_commands.update_chapter(chapter_id, body)

    def delete_chapter(self, chapter_id):
        return self.catalog_commands.delete_chapter(chapter_id)

    def reorder_chapters(self, book_id, chapter_ids):
        return self.catalog_commands.reorder_chapters(book_id, chapter_ids)

    async def replan_chapters(self, book_id):
        return await self.book_planning.propose(book_id)

    def confirm_replan(self, book_id, proposal_id):
        return self.book_planning.confirm(book_id, proposal_id)

    async def generate_chapter(
        self,
        chapter_id,
        *,
        first_section_status="available",
    ):
        return await self.chapter_planning.generate(
            chapter_id,
            first_section_status=first_section_status,
        )

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
                retryable=True,
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

    async def prepare_section(self, section_id: str):
        """Foreground escape hatch for an unlocked section with missing content.

        It repairs only orphaned orchestration state. Content still has to pass
        the normal generation coordinator and is frozen only by open_section.
        """

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
        self._release_orphaned_preparing(progress)
        if progress.status == "preparing":
            raise AppError(
                "本节仍在准备中，请等待当前任务完成",
                code="SECTION_PREPARING",
                status=409,
            )
        await self.generate_section(section_id)
        return self.open_section(section_id)

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
        self._release_orphaned_preparing(progress)
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
        view = self.section(section_id)
        snapshot = self.daily_modes.activity_snapshot()
        if snapshot:
            view.update(snapshot)
        return view

    def _release_orphaned_preparing(self, progress) -> None:
        """Restore an orphaned preparation projection without publishing data."""

        if progress.status != "preparing":
            return
        active_tasks = self.db.scalars(
            select(LearningTask).where(
                LearningTask.learning_run_id == progress.learning_run_id,
                LearningTask.user_id == self.user_id,
                LearningTask.status.in_({"pending", "running"}),
                LearningTask.task_type.in_({
                    "initial_book_preload",
                    "next_section_preload",
                }),
            )
        ).all()
        has_owner = False
        for task in active_tasks:
            payload = load(task.payload_json, {}) or {}
            if str(payload.get("targetSectionId") or "") == progress.section_id:
                has_owner = True
                break
            # Compatibility for tasks created before targetSectionId became
            # part of the quiz-pass transaction. The source deterministically
            # identifies the next existing section while the task is active.
            source_id = str(payload.get("sourceSectionId") or "")
            source = self.db.get(Section, source_id) if source_id else None
            if (
                task.task_type == "next_section_preload"
                and source
                and (target := self._next_existing_section(source))
                and target.id == progress.section_id
            ):
                has_owner = True
                break
        if has_owner:
            return
        self.progress.set_status(progress, "available")
        self.db.commit()

    def section(self, section_id):
        return self.section_reads.get(
            section_id,
            allow_preparing=isinstance(self.scope, WorkerExecutionContext),
        )

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
                    task,
                    aggregate.chapter.id,
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
                    task,
                    payload.get("sourceSectionId") or task.section_id
                )
            elif task.task_type == "section_lookahead_preload":
                result = await self._preload_lookahead_section(
                    task,
                    payload.get("sourceSectionId") or task.section_id,
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

    async def _preload_initial_book(self, task: LearningTask, chapter_id):
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
        payload = load(task.payload_json, {})
        payload["targetSectionId"] = target.id
        task.payload_json = dump(payload)
        if target_progress.status != "preparing":
            self.progress.set_status(target_progress, "preparing")
        self.db.commit()
        await self.generate_section(target.id)
        self.progress.set_status(target_progress, "available")
        self._enqueue_lookahead(task, target)
        self.db.commit()
        return {"targetSectionId": target.id}

    def _next_existing_section(self, source: Section) -> Section | None:
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=source.id,
        )
        target = self.db.scalar(
            select(Section)
            .where(
                Section.chapter_id == context.chapter.id,
                Section.position > source.position,
            )
            .order_by(Section.position)
        )
        if target:
            return target
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
        if not next_chapter:
            return None
        return self.db.scalar(
            select(Section)
            .where(Section.chapter_id == next_chapter.id)
            .order_by(Section.position)
        )

    def _enqueue_lookahead(self, parent: LearningTask, source: Section) -> None:
        existing = self.db.scalar(
            select(LearningTask).where(
                LearningTask.learning_run_id == parent.learning_run_id,
                LearningTask.task_type == "section_lookahead_preload",
                LearningTask.idempotency_key == f"lookahead-after:{source.id}",
            )
        )
        if existing:
            return
        self.db.add(LearningTask(
            id=uid("task"),
            learning_run_id=parent.learning_run_id,
            section_id=source.id,
            user_id=parent.user_id,
            task_type="section_lookahead_preload",
            idempotency_key=f"lookahead-after:{source.id}",
            trigger_id=parent.id,
            payload_json=dump({"sourceSectionId": source.id}),
            status="pending",
        ))

    async def _generate_preload_target(self, section_id: str) -> dict:
        """Let an unlock task adopt an in-flight lookahead without racing it."""

        for _ in range(60):
            try:
                return await self.generate_section(section_id)
            except AppError as error:
                if error.code != "GENERATION_IN_PROGRESS":
                    raise
                await sleep(0.5)
                self.db.expire_all()
        raise AppError(
            "本节预热仍在运行，稍后继续确认",
            code="GENERATION_IN_PROGRESS",
            status=409,
            retryable=True,
        )

    async def _preload_lookahead_section(
        self,
        task: LearningTask,
        source_section_id: str,
    ):
        source = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=source_section_id,
        ).section
        target = self._next_existing_section(source)
        if not target:
            return {"targetSectionId": None, "endOfAvailableRoute": True}
        target_progress = self.progress.for_section(target)
        # This task is a content buffer only. It must never unlock or mark the
        # target as preparing, because access is still owned by progression.
        if target_progress.status == "completed":
            return {"targetSectionId": target.id, "alreadyCompleted": True}
        await self._generate_preload_target(target.id)
        return {"targetSectionId": target.id, "endOfAvailableRoute": False}

    async def _preload_next_section(
        self,
        task: LearningTask,
        source_section_id,
    ):
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
        payload = load(task.payload_json, {})
        payload["targetSectionId"] = target.id
        task.payload_json = dump(payload)
        if target_progress.status != "preparing":
            self.progress.set_status(target_progress, "preparing")
        self.db.commit()
        await self._generate_preload_target(target.id)
        self.progress.set_status(target_progress, "available")
        self._enqueue_lookahead(task, target)
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
        result_payload = dict(result)
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
            result_json=dump(result_payload),
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
        if evidence_type in {"ask_me", "ask_me_topic"}:
            target_ids = [
                str(item)
                for item in result_payload.get("assessmentTargetIds", [])
                if str(item)
            ]
            source_id = str(
                result_payload.get("topicId")
                or (
                    f"{result_payload.get('sessionId', '')}:"
                    f"{result_payload.get('dimension', '')}"
                )
            )
            contract_version_id = str(
                result_payload.get("learningContractVersionId") or ""
            )
            record_ask_me_assessment_facts(
                self.db,
                learning_run_id=evidence.learning_run_id,
                user_id=self.user_id,
                section_id=context["section"].id,
                learning_contract_version_id=contract_version_id,
                content_version_id=(
                    str(result_payload.get("contentVersionId"))
                    if result_payload.get("contentVersionId")
                    else None
                ),
                assessment_target_ids=target_ids,
                source_type=evidence_type,
                source_id=source_id,
                evaluation=str(result_payload.get("evaluation") or ""),
                dimension=str(result_payload.get("dimension") or ""),
                payload=result_payload,
            )

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
        node_views = knowledge_node_views_for_targets(
            self.db,
            user_id=self.user_id,
            target_ids=set(target_ids),
        )
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
        result = []
        for state, target in projection_rows:
            node = node_views.get(target.concept_revision_id or "")
            teaching_action = (
                "wake"
                if node and node["activation"] == "due"
                else "scaffold"
                if node and node["activation"] == "reassessment"
                else "compress"
                if node and node["activation"] == "active" and node["rankOrder"] >= 3
                else "connect"
                if node and node["activation"] == "active"
                else "teach"
            )
            result.append({
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
                **({
                    "knowledgeNode": {
                        "conceptRevisionId": node["conceptRevisionId"],
                        "capabilityScope": node["capabilityScope"],
                        "rank": node["rank"],
                        "rankLabel": node["rankLabel"],
                        "rankCeiling": node["rankCeiling"],
                        "activation": node["activation"],
                        "evidenceCount": node["evidenceCount"],
                    },
                    "teachingAction": teaching_action,
                } if node else {"teachingAction": "teach"}),
            })

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

    def knowledge_map(self, series_id: str | None = None):
        return KnowledgeMapService(
            self.db,
            user_id=self.user_id,
        ).view(series_id=series_id)

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

    async def start_review_reinforcement(self, assignment_id: str):
        return await ReinforcementService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
        ).start_for_review(assignment_id)

    async def start_target_reinforcement(self, target_id: str):
        return await ReinforcementService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
        ).start_for_target(target_id)

    def reinforcement_run(self, run_id: str):
        return ReinforcementService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
        ).view(run_id)

    def active_reinforcement(self):
        return ReinforcementService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
        ).active()

    def respond_reinforcement(self, run_id: str, body, idempotency_key=None):
        return ReinforcementService(
            self.db,
            user_id=self.user_id,
            ai=self.ai,
        ).respond(run_id, body, idempotency_key=idempotency_key)

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
        return await self.notes.ensure(section)

    def _note(self, note):
        return self.notes.view(note)

    def update_note(self, section_id, content):
        return self.notes.update(section_id, content)

    def add_note_review_supplement(
        self,
        section_id: str,
        review_episode_id: str,
        content: dict,
    ):
        return self.notes.add_review_supplement(
            section_id,
            review_episode_id,
            content,
        )

    def prepare_ask(self, section_id, body):
        return self.qa_service.prepare(section_id, body)

    def qa_history(self, section_id):
        return self.qa_service.history(section_id)

    def _save_qa_answer(
        self,
        context,
        body,
        answer,
        suggested_relation,
        thread_summary="",
    ):
        return self.qa_service.save_answer(
            context,
            body,
            answer,
            suggested_relation,
            thread_summary,
        )

    async def ask(self, section_id, body):
        return await self.qa_service.ask(section_id, body)

    async def ask_stream(self, context, body):
        async for event in self.qa_service.stream(context, body):
            yield event

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
        return self.qa_service.correct_classification(
            section_id,
            thread_id,
            body,
        )

    async def ask_me(self, section_id, answer):
        return await self.ask_me_service.answer(section_id, answer)

    def ask_me_discussion(self, section_id):
        return self.ask_me_service.discussion(section_id)

    def start_ask_me_discussion(self, section_id):
        return self.ask_me_service.start_discussion(section_id)

    async def submit_ask_me_discussion_turn(
        self,
        section_id,
        body,
        idempotency_key,
    ):
        return await self.ask_me_service.submit_discussion_turn(
            section_id,
            body,
            idempotency_key,
        )

    def apply_ask_me_discussion_action(
        self,
        section_id,
        body,
        idempotency_key,
    ):
        return self.ask_me_service.discussion_action(
            section_id,
            body,
            idempotency_key,
        )

    def _ask_me(self, session):
        return self.ask_me_service.view(session)

    def chapter_practice(self, chapter_id):
        return self.artifact_service.chapter_practice(chapter_id)

    def upload_chapter_practice_attachment(
        self, chapter_id, filename, media_type, data
    ):
        return self.artifact_service.upload_chapter_practice_attachment(
            chapter_id, filename, media_type, data
        )

    def submit_chapter_practice(self, chapter_id, content, attachment_ids):
        return self.artifact_service.submit_chapter_practice(
            chapter_id, content, attachment_ids
        )

    def _practice_progress(self, practice):
        return self.artifact_service.practice_progress(practice)

    def _practice(self, practice):
        return self.artifact_service.practice_view(practice)

    def book_capstone(self, book_id):
        return self.artifact_service.book_capstone(book_id)

    def upload_book_capstone_attachment(
        self, book_id, filename, media_type, data
    ):
        return self.artifact_service.upload_book_capstone_attachment(
            book_id, filename, media_type, data
        )

    def submit_book_capstone(self, book_id, content, attachment_ids):
        return self.artifact_service.submit_book_capstone(
            book_id, content, attachment_ids
        )

    def _capstone_progress(self, capstone):
        return self.artifact_service.capstone_progress(capstone)

    def _capstone(self, capstone):
        return self.artifact_service.capstone_view(capstone)

    def attachment(self, attachment_id):
        return self.artifact_service.attachment(attachment_id)

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
