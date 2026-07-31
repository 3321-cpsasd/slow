import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import LearningTask, now


RUNNING_LEASE = timedelta(minutes=5)
TASK_TYPES = {
    "initial_book_preload",
    "note_generation",
    "remediation_generation",
    "next_section_preload",
}


def _load(value: str, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def recoverable_task_ids(db: Session, *, limit: int = 20) -> list[str]:
    cutoff = datetime.now(timezone.utc) - RUNNING_LEASE
    return list(
        db.scalars(
            select(LearningTask.id)
            .where(
                LearningTask.attempt_count < LearningTask.max_attempts,
                or_(
                    LearningTask.status == "pending",
                    (
                        (LearningTask.status == "running")
                        & (LearningTask.updated_at < cutoff)
                    ),
                ),
            )
            .order_by(LearningTask.created_at)
            .limit(limit)
        ).all()
    )


def claim_task(db: Session, task_id: str) -> LearningTask | None:
    task = db.get(LearningTask, task_id)
    if not task or task.status == "succeeded":
        return None
    if task.attempt_count >= task.max_attempts:
        return None
    if task.status == "running":
        updated_at = task.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated_at < RUNNING_LEASE:
            return None
    if task.task_type not in TASK_TYPES:
        task.status = "failed"
        task.error_code = "TASK_TYPE_UNSUPPORTED"
        task.error_message = "Unsupported durable task type"
        task.attempt_count = task.max_attempts
        task.updated_at = now()
        db.commit()
        return None
    task.status = "running"
    task.attempt_count += 1
    task.error_code = ""
    task.error_message = ""
    task.updated_at = now()
    db.commit()
    return task


def complete_task(db: Session, task_id: str, result: dict | None = None) -> LearningTask:
    task = db.get(LearningTask, task_id)
    if not task:
        raise AppError("学习任务不存在", code="LEARNING_TASK_NOT_FOUND", status=404)
    task.status = "succeeded"
    task.result_json = json.dumps(result or {}, ensure_ascii=False)
    task.error_code = ""
    task.error_message = ""
    task.updated_at = now()
    db.commit()
    return task


def fail_task(db: Session, task_id: str, error: Exception) -> LearningTask:
    db.rollback()
    task = db.get(LearningTask, task_id)
    if not task:
        raise AppError("学习任务不存在", code="LEARNING_TASK_NOT_FOUND", status=404)
    task.status = "failed"
    task.error_code = getattr(error, "code", type(error).__name__)[:80]
    task.error_message = f"{type(error).__name__}: learning task failed"
    task.updated_at = now()
    db.commit()
    return task


def release_task(db: Session, task_id: str) -> None:
    """Return interrupted work to the queue without consuming a retry."""
    db.rollback()
    task = db.get(LearningTask, task_id)
    if not task or task.status != "running":
        return
    task.status = "pending"
    task.attempt_count = max(0, task.attempt_count - 1)
    task.updated_at = now()
    db.commit()


def reset_failed_task(db: Session, task: LearningTask) -> LearningTask:
    if task.status != "failed":
        raise AppError(
            "只有失败任务可以重试",
            code="LEARNING_TASK_NOT_FAILED",
            status=409,
        )
    if task.attempt_count >= task.max_attempts:
        raise AppError(
            "学习任务已达到最大重试次数",
            code="LEARNING_TASK_RETRY_EXHAUSTED",
            status=409,
        )
    task.status = "pending"
    task.error_code = ""
    task.error_message = ""
    task.updated_at = now()
    db.commit()
    return task


def task_view(task: LearningTask) -> dict:
    return {
        "taskId": task.id,
        "type": task.task_type,
        "sectionId": task.section_id,
        "status": task.status,
        "attemptCount": task.attempt_count,
        "maxAttempts": task.max_attempts,
        "retryable": (
            task.status == "failed" and task.attempt_count < task.max_attempts
        ),
        "errorCode": task.error_code or None,
        "result": _load(task.result_json, {}),
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
    }
