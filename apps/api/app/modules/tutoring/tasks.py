from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import NoteGenerationTask, Section, now
from ...platform.unit_of_work import SqlAlchemyUnitOfWork

MAX_NOTE_ATTEMPTS = 3
RUNNING_LEASE = timedelta(minutes=5)


def recoverable_note_tasks(db: Session) -> list[NoteGenerationTask]:
    cutoff = datetime.now(timezone.utc) - RUNNING_LEASE
    return list(
        db.scalars(
            select(NoteGenerationTask)
            .where(
                NoteGenerationTask.attempt_count < MAX_NOTE_ATTEMPTS,
                or_(
                    NoteGenerationTask.status.in_(["pending", "failed"]),
                    (
                        (NoteGenerationTask.status == "running")
                        & (NoteGenerationTask.updated_at < cutoff)
                    ),
                ),
            )
            .order_by(NoteGenerationTask.created_at)
        ).all()
    )


async def execute_note_task(
    db: Session,
    *,
    task_id: str,
    section: Section,
    note_generator: Callable[[Section], Awaitable[None]],
) -> NoteGenerationTask:
    uow = SqlAlchemyUnitOfWork(db)
    task = db.get(NoteGenerationTask, task_id)
    if not task:
        raise AppError("笔记任务不存在", code="NOTE_TASK_NOT_FOUND", status=404)
    if task.status == "succeeded":
        return task
    if task.attempt_count >= MAX_NOTE_ATTEMPTS:
        raise AppError(
            "笔记任务已达到最大重试次数",
            code="NOTE_TASK_RETRY_EXHAUSTED",
            status=409,
        )
    task.status = "running"
    task.attempt_count += 1
    task.error_code = ""
    task.error_message = ""
    task.updated_at = now()
    uow.commit()
    try:
        await note_generator(section)
        task = db.get(NoteGenerationTask, task_id)
        task.status = "succeeded"
        task.updated_at = now()
        uow.commit()
        return task
    except Exception as error:
        uow.rollback()
        task = db.get(NoteGenerationTask, task_id)
        task.status = "failed"
        task.error_code = getattr(error, "code", type(error).__name__)[:80]
        # Persist a safe diagnostic category, never the raw upstream message.
        task.error_message = f"{type(error).__name__}: note generation failed"
        task.updated_at = now()
        uow.commit()
        return task
