import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from ...auth.context import Principal, WorkerExecutionContext
from ...core.errors import AppError, safe_error_code
from ...infrastructure.tables import (
    Book,
    Chapter,
    ContentVersion,
    LearningRun,
    LearningTask,
    QuizSet,
    Section,
    SectionProgress,
    now,
)


RUNNING_LEASE = timedelta(seconds=90)
TASK_TYPES = {
    "content_feedback_regeneration",
    "initial_book_preload",
    "note_generation",
    "remediation_generation",
    "next_section_preload",
    "section_lookahead_preload",
}
PRELOAD_TASK_TYPES = {"initial_book_preload", "next_section_preload"}
MANUALLY_EXTENSIBLE_TASK_TYPES = PRELOAD_TASK_TYPES | {"remediation_generation"}
MANUAL_TASK_RETRY_BUDGET = 3


def backfill_missing_lookahead_tasks(db: Session) -> int:
    """Queue the missing one-section buffer within each active book.

    This repairs orchestration only. The target remains locked until normal
    progression unlocks it, and the lookahead worker still has to publish a
    complete content/quiz pair through the standard generation boundary. A
    chapter boundary does not stop the buffer; a book boundary does.
    """

    published_pair_exists = (
        select(QuizSet.id)
        .join(ContentVersion, ContentVersion.id == QuizSet.content_version_id)
        .where(
            QuizSet.section_id == Section.id,
            QuizSet.publication_status == "published",
            ContentVersion.section_id == Section.id,
            ContentVersion.publication_status == "published",
        )
        .exists()
    )
    candidates = db.execute(
        select(
            LearningRun.id,
            LearningRun.user_id,
            Section.id,
            Book.id,
            Chapter.position,
            Section.position,
        )
        .join(
            SectionProgress,
            and_(
                SectionProgress.learning_run_id == LearningRun.id,
                SectionProgress.user_id == LearningRun.user_id,
            ),
        )
        .join(Section, Section.id == SectionProgress.section_id)
        .join(Chapter, Chapter.id == Section.chapter_id)
        .join(Book, Book.id == Chapter.book_id)
        .where(
            LearningRun.status == "active",
            SectionProgress.status == "available",
            Book.series_id == LearningRun.series_id,
            Book.deleted_at.is_(None),
            published_pair_exists,
        )
    ).all()

    created = 0
    for (
        learning_run_id,
        user_id,
        source_section_id,
        book_id,
        chapter_position,
        section_position,
    ) in candidates:
        idempotency_key = f"lookahead-after:{source_section_id}"
        existing = db.scalar(
            select(LearningTask.id).where(
                LearningTask.learning_run_id == learning_run_id,
                LearningTask.task_type == "section_lookahead_preload",
                LearningTask.idempotency_key == idempotency_key,
            )
        )
        if existing:
            continue

        later_section_exists = db.scalar(
            select(Section.id)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .where(
                Chapter.book_id == book_id,
                or_(
                    Chapter.position > chapter_position,
                    and_(
                        Chapter.position == chapter_position,
                        Section.position > section_position,
                    ),
                ),
            )
            .order_by(Chapter.position, Section.position)
            .limit(1)
        )
        later_chapter_exists = db.scalar(
            select(Chapter.id)
            .where(
                Chapter.book_id == book_id,
                Chapter.position > chapter_position,
            )
            .order_by(Chapter.position)
            .limit(1)
        )
        if not later_section_exists and not later_chapter_exists:
            continue

        db.add(LearningTask(
            id=f"task_{uuid4().hex}",
            learning_run_id=learning_run_id,
            section_id=source_section_id,
            user_id=user_id,
            task_type="section_lookahead_preload",
            idempotency_key=idempotency_key,
            trigger_id=f"startup-backfill:{source_section_id}",
            payload_json=json.dumps(
                {"sourceSectionId": source_section_id},
                ensure_ascii=False,
            ),
            status="pending",
        ))
        created += 1

    if created:
        db.commit()
    return created


def _load(value: str, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _restore_terminal_preload_progress(db: Session, task: LearningTask) -> None:
    """Make an unlocked preload target user-recoverable after terminal failure."""

    if task.task_type not in PRELOAD_TASK_TYPES:
        return
    target_section_id = str(
        (_load(task.payload_json, {}) or {}).get("targetSectionId") or ""
    )
    if not target_section_id:
        return
    db.execute(
        update(SectionProgress)
        .where(
            SectionProgress.learning_run_id == task.learning_run_id,
            SectionProgress.user_id == task.user_id,
            SectionProgress.section_id == target_section_id,
            SectionProgress.status == "preparing",
        )
        .values(status="available", updated_at=now())
    )


def recoverable_task_ids(db: Session, *, limit: int = 20) -> list[str]:
    current = datetime.now(timezone.utc)
    exhausted_tasks = db.scalars(
        select(LearningTask).where(
            LearningTask.status == "running",
            LearningTask.attempt_count >= LearningTask.max_attempts,
            or_(
                LearningTask.lease_expires_at.is_(None),
                LearningTask.lease_expires_at < current,
            ),
        )
    ).all()
    exhausted = db.execute(
        update(LearningTask)
        .where(
            LearningTask.status == "running",
            LearningTask.attempt_count >= LearningTask.max_attempts,
            or_(
                LearningTask.lease_expires_at.is_(None),
                LearningTask.lease_expires_at < current,
            ),
        )
        .values(
            status="failed",
            error_code="LEARNING_TASK_RETRY_EXHAUSTED",
            error_message="后台任务在执行中断后已达到最大尝试次数",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            updated_at=now(),
        )
    )
    if exhausted.rowcount:
        for task in exhausted_tasks:
            _restore_terminal_preload_progress(db, task)
        db.commit()
    return list(
        db.scalars(
            select(LearningTask.id)
            .where(
                LearningTask.attempt_count < LearningTask.max_attempts,
                or_(
                    LearningTask.status == "pending",
                    (
                        (LearningTask.status == "running")
                        & or_(
                            LearningTask.lease_expires_at.is_(None),
                            LearningTask.lease_expires_at < current,
                        )
                    ),
                ),
            )
            .order_by(LearningTask.created_at)
            .limit(limit)
        ).all()
    )


def claim_task(
    db: Session,
    task_id: str,
    *,
    lease_owner: str,
) -> WorkerExecutionContext | None:
    task = db.get(LearningTask, task_id)
    if not task or task.status == "succeeded":
        return None
    if task.attempt_count >= task.max_attempts:
        return None
    if task.status == "running":
        expires_at = task.lease_expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at >= datetime.now(timezone.utc):
            return None
    if task.task_type not in TASK_TYPES:
        task.status = "failed"
        task.error_code = "TASK_TYPE_UNSUPPORTED"
        task.error_message = "Unsupported durable task type"
        task.attempt_count = task.max_attempts
        task.updated_at = now()
        db.commit()
        return None
    current = now()
    lease_token = uuid4().hex
    claimed = db.execute(
        update(LearningTask)
        .where(
            LearningTask.id == task_id,
            LearningTask.attempt_count < LearningTask.max_attempts,
            or_(
                LearningTask.status == "pending",
                (
                    (LearningTask.status == "running")
                    & or_(
                        LearningTask.lease_expires_at.is_(None),
                        LearningTask.lease_expires_at < current,
                    )
                ),
            ),
        )
        .values(
            status="running",
            attempt_count=LearningTask.attempt_count + 1,
            error_code="",
            error_message="",
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=current + RUNNING_LEASE,
            heartbeat_at=current,
            updated_at=current,
        )
    )
    if claimed.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return WorkerExecutionContext(
        principal=Principal(
            actor_kind="system_worker",
            actor_id=lease_owner,
            subject_user_id=task.user_id,
            session_id=None,
        ),
        task_id=task_id,
        lease_owner=lease_owner,
        lease_token=lease_token,
    )


def heartbeat_task(
    db: Session,
    context: WorkerExecutionContext,
) -> bool:
    current = now()
    heartbeat = db.execute(
        update(LearningTask)
        .where(
            LearningTask.id == context.task_id,
            LearningTask.status == "running",
            LearningTask.lease_owner == context.lease_owner,
            LearningTask.lease_token == context.lease_token,
        )
        .values(
            heartbeat_at=current,
            lease_expires_at=current + RUNNING_LEASE,
            updated_at=current,
        )
    )
    db.commit()
    return heartbeat.rowcount == 1


def complete_task(
    db: Session,
    context: WorkerExecutionContext,
    result: dict | None = None,
) -> LearningTask:
    completed = db.execute(
        update(LearningTask)
        .where(
            LearningTask.id == context.task_id,
            LearningTask.status == "running",
            LearningTask.lease_owner == context.lease_owner,
            LearningTask.lease_token == context.lease_token,
        )
        .values(
            status="succeeded",
            result_json=json.dumps(result or {}, ensure_ascii=False),
            error_code="",
            error_message="",
            updated_at=now(),
        )
    )
    if completed.rowcount != 1:
        db.rollback()
        raise AppError(
            "任务租约已失效，拒绝提交旧 Worker 的结果",
            code="TASK_LEASE_LOST",
            status=409,
        )
    db.commit()
    task = db.get(LearningTask, context.task_id)
    return task


def fail_task(
    db: Session,
    context: WorkerExecutionContext,
    error: Exception,
) -> LearningTask:
    db.rollback()
    task = db.get(LearningTask, context.task_id)
    retry_automatically = bool(
        task
        and getattr(error, "retryable", False)
        and task.attempt_count < task.max_attempts
    )
    safe_message = (
        str(error)[:500]
        if isinstance(error, AppError)
        else "后台任务执行失败，请安全重试"
    )
    failed = db.execute(
        update(LearningTask)
        .where(
            LearningTask.id == context.task_id,
            LearningTask.status == "running",
            LearningTask.lease_owner == context.lease_owner,
            LearningTask.lease_token == context.lease_token,
        )
        .values(
            status="pending" if retry_automatically else "failed",
            error_code=safe_error_code(error),
            error_message=safe_message,
            lease_owner=None if retry_automatically else context.lease_owner,
            lease_token=None if retry_automatically else context.lease_token,
            lease_expires_at=None if retry_automatically else task.lease_expires_at,
            heartbeat_at=None if retry_automatically else task.heartbeat_at,
            updated_at=now(),
        )
    )
    if failed.rowcount != 1:
        db.rollback()
        raise AppError(
            "任务租约已失效，拒绝旧 Worker 覆盖任务状态",
            code="TASK_LEASE_LOST",
            status=409,
        )
    if not retry_automatically:
        _restore_terminal_preload_progress(db, task)
    db.commit()
    task = db.get(LearningTask, context.task_id)
    return task


def release_task(db: Session, context: WorkerExecutionContext) -> None:
    """Return interrupted work to the queue without consuming a retry."""
    db.rollback()
    released = db.execute(
        update(LearningTask)
        .where(
            LearningTask.id == context.task_id,
            LearningTask.status == "running",
            LearningTask.lease_owner == context.lease_owner,
            LearningTask.lease_token == context.lease_token,
        )
        .values(
            status="pending",
            attempt_count=LearningTask.attempt_count - 1,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            updated_at=now(),
        )
    )
    if released.rowcount != 1:
        db.rollback()
        return
    db.commit()


def reset_failed_task(db: Session, task: LearningTask) -> LearningTask:
    if task.status != "failed":
        raise AppError(
            "只有失败任务可以重试",
            code="LEARNING_TASK_NOT_FAILED",
            status=409,
        )
    if task.attempt_count >= task.max_attempts:
        if task.task_type not in MANUALLY_EXTENSIBLE_TASK_TYPES:
            raise AppError(
                "学习任务已达到最大重试次数",
                code="LEARNING_TASK_RETRY_EXHAUSTED",
                status=409,
            )
        # Preserve the cumulative attempt count and extend the audited budget
        # instead of resetting history when a user explicitly retries.
        task.max_attempts = task.attempt_count + MANUAL_TASK_RETRY_BUDGET
    task.status = "pending"
    task.error_code = ""
    task.error_message = ""
    task.lease_owner = None
    task.lease_token = None
    task.lease_expires_at = None
    task.heartbeat_at = None
    task.updated_at = now()
    db.commit()
    return task


def task_view(task: LearningTask) -> dict:
    return {
        "taskId": task.id,
        "type": task.task_type,
        "sectionId": task.section_id,
        "triggerId": task.trigger_id,
        "status": task.status,
        "attemptCount": task.attempt_count,
        "maxAttempts": task.max_attempts,
        "retryable": (
            task.status == "failed"
            and (
                task.attempt_count < task.max_attempts
                or task.task_type in MANUALLY_EXTENSIBLE_TASK_TYPES
            )
        ),
        "errorCode": task.error_code or None,
        "errorMessage": task.error_message or None,
        "result": _load(task.result_json, {}),
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
    }
