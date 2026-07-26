from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import ArtifactProgress, now
from ..learning.domain import ProgressionDecision


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ArtifactProgressStore:
    """The sole writer for per-learning-run artifact state and submissions."""

    def __init__(self, db: Session, *, user_id: str):
        self.db = db
        self.user_id = user_id

    def add(
        self,
        *,
        learning_run_id: str,
        target_type: str,
        target_id: str,
        status: str = "locked",
        submission_json: str = "{}",
    ) -> ArtifactProgress:
        row = ArtifactProgress(
            id=_uid("artifact_progress"),
            learning_run_id=learning_run_id,
            user_id=self.user_id,
            target_type=target_type,
            target_id=target_id,
            status=status,
            submission_json=submission_json,
        )
        self.db.add(row)
        return row

    def for_target(
        self,
        *,
        learning_run_id: str,
        target_type: str,
        target_id: str,
    ) -> ArtifactProgress:
        row = self.db.scalar(
            select(ArtifactProgress).where(
                ArtifactProgress.learning_run_id == learning_run_id,
                ArtifactProgress.target_type == target_type,
                ArtifactProgress.target_id == target_id,
            )
        )
        if row:
            return row
        raise AppError(
            "成果进度投影缺失",
            code="ARTIFACT_PROGRESS_MISSING",
            status=500,
        )

    def apply_availability(
        self,
        *,
        learning_run_id: str,
        decision: ProgressionDecision,
    ) -> None:
        if decision.available_practice_id:
            row = self.for_target(
                learning_run_id=learning_run_id,
                target_type="chapter_practice",
                target_id=decision.available_practice_id,
            )
            if row.status == "locked":
                row.status = "available"
                row.updated_at = now()
        if decision.available_capstone_id:
            row = self.for_target(
                learning_run_id=learning_run_id,
                target_type="book_capstone",
                target_id=decision.available_capstone_id,
            )
            if row.status == "locked":
                row.status = "available"
                row.updated_at = now()
