import hashlib
import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...ai.port import AiPort
from ...core.errors import AppError, safe_error_code
from ...infrastructure.tables import (
    Book,
    BookCapstone,
    Chapter,
    LearningPlan,
    LearningTask,
    PlanCreationRequest,
    Series,
    now,
)
from .content_safety import require_safe_generated_plan, require_safe_plan_request


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class SeriesPlanningService:
    """Creates the complete Series route without planning chapter sections."""

    def __init__(
        self,
        db: Session,
        ai: AiPort,
        *,
        user_id: str,
        progress,
        artifacts,
        missions,
        milestones,
        baselines,
        learning_start,
        generation_contexts,
        shelf_provider: Callable[[str], object],
        memory_provider: Callable[[str], list[dict]],
        series_view: Callable[[str], dict],
    ):
        self.db = db
        self.ai = ai
        self.user_id = user_id
        self.progress = progress
        self.artifacts = artifacts
        self.missions = missions
        self.milestones = milestones
        self.baselines = baselines
        self.learning_start = learning_start
        self.generation_contexts = generation_contexts
        self.shelf_provider = shelf_provider
        self.memory_provider = memory_provider
        self.series_view = series_view

    async def create(self, body, idempotency_key: str | None = None) -> dict:
        shelf = self.shelf_provider(body.shelf_id)
        require_safe_plan_request(body)
        request = self.learning_start.plan_payload(body)
        learning_start_context = self.learning_start.planning_context(body)
        memory = self.memory_provider(body.shelf_id)
        baseline = self.baselines.select_for_plan(
            shelf=shelf,
            plan_input=request,
        )
        baseline_context = (
            self.baselines.planning_context(baseline) if baseline else {}
        )
        context_pack = self.generation_contexts.build(
            "plan",
            shelf=shelf,
            memory=memory,
            plan_input=request,
            curriculum_baseline=baseline_context,
        )
        ai_request = self.generation_contexts.attach(
            {
                **request,
                "learningStart": learning_start_context,
            },
            context_pack,
        )
        request_key = (idempotency_key or _uid("plan_request")).strip()
        if len(request_key) < 8 or len(request_key) > 128:
            raise AppError(
                "创建请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        request_hash = hashlib.sha256(
            json.dumps(
                ai_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
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
        if (
            not reservation
            or reservation.user_id != self.user_id
            or reservation.request_hash != request_hash
        ):
            raise AppError(
                "创建请求标识已用于其他学习计划",
                code="IDEMPOTENCY_KEY_REUSED",
                status=409,
            )
        if reservation.status == "completed" and reservation.series_id:
            return self.series_view(reservation.series_id)
        if reservation.status == "failed":
            reservation.status = "pending"
            reservation.error_code = ""
            reservation.updated_at = now()
            self.db.commit()
            owns_reservation = True
        elif not owns_reservation:
            raise AppError(
                "相同学习计划正在生成，请勿重复提交",
                code="PLAN_CREATION_IN_PROGRESS",
                status=409,
            )

        try:
            self.db.commit()
            generated = await self.ai.plan(ai_request, memory)
            require_safe_generated_plan(generated)
            if baseline:
                self.baselines.validate_plan_coverage(baseline, generated)
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
            id=_uid("plan"),
            **request,
            assumptions_json=_dump(generated.assumptions),
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
            id=_uid("series"),
            plan_id=plan.id,
            shelf_id=body.shelf_id,
            title=generated.series_title,
            rationale=generated.rationale,
            initial_mission_version_id=mission.id,
        )
        self.db.add(series)
        self.db.flush()
        self.learning_start.bind_series(series_id=series.id, body=body)
        if baseline:
            self.baselines.bind_series(
                series_id=series.id,
                baseline=baseline,
                plan_input=request,
            )
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

        first_chapter_id = None
        milestone_chapters = {}
        for book_position, item in enumerate(generated.books, 1):
            book = Book(
                id=_uid("book"),
                series_id=series.id,
                shelf_id=body.shelf_id,
                position=book_position,
                title=item.title,
                topic=item.topic,
                description=item.description,
                estimated_minutes=item.estimated_minutes,
                outline_status=(
                    "confirmed" if book_position == 1 else "draft"
                ),
                outline_version=1,
                outline_confirmed_at=(now() if book_position == 1 else None),
            )
            self.db.add(book)
            self.db.flush()
            self.progress.add_book(
                learning_run,
                book,
                status="available" if book_position == 1 else "locked",
            )
            capstone = BookCapstone(
                id=_uid("capstone"),
                book_id=book.id,
                title=f"《{book.title}》全书大作业",
                brief_json=_dump(
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
            for chapter_position, item_chapter in enumerate(item.chapters, 1):
                chapter = Chapter(
                    id=_uid("chapter"),
                    book_id=book.id,
                    position=chapter_position,
                    title=item_chapter.title,
                    objective=item_chapter.objective,
                    knowledge_identity_scope_json="{}",
                )
                self.db.add(chapter)
                self.db.flush()
                if baseline:
                    self.baselines.bind_chapter_objectives(
                        chapter_id=chapter.id,
                        baseline=baseline,
                        objective_keys=item_chapter.baseline_objective_ids,
                    )
                    from ..knowledge.fact_graph import KnowledgeFactGraphService

                    KnowledgeFactGraphService(self.db).bind_chapter_identity_scope(
                        chapter_id=chapter.id,
                        baseline_version_id=baseline.id,
                        objective_keys=item_chapter.baseline_objective_ids,
                        concept_keys=item_chapter.baseline_concept_ids,
                    )
                milestone_chapters[(book_position, chapter_position)] = (
                    chapter,
                    book,
                )
                self.progress.add_chapter(
                    learning_run,
                    chapter,
                    status=(
                        "available"
                        if book_position == 1 and chapter_position == 1
                        else "locked"
                    ),
                )
                if book_position == 1 and chapter_position == 1:
                    first_chapter_id = chapter.id

        self.milestones.create_for_plan(
            series_id=series.id,
            generated=generated,
            chapter_map=milestone_chapters,
        )
        if first_chapter_id:
            self.db.add(
                LearningTask(
                    id=_uid("task"),
                    learning_run_id=learning_run.id,
                    section_id=None,
                    user_id=self.user_id,
                    task_type="initial_book_preload",
                    idempotency_key=f"initial-book:{series.id}",
                    trigger_id=plan.id,
                    payload_json=_dump({"chapterId": first_chapter_id}),
                    status="pending",
                )
            )
        self.db.commit()
        return self.series_view(series.id)
