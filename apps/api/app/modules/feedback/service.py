import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...api.schemas import FeedbackCreate
from ...auth.context import UserScope
from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentAnswerVersion,
    AssessmentItemVersion,
    Book,
    Chapter,
    ContentVersion,
    LearningTask,
    QuizAttempt,
    QuizSet,
    Section,
    Series,
    Shelf,
    UserFeedback,
)
from ..learning.tasks import task_view


FEEDBACK_PER_MINUTE_LIMIT = 8
FEEDBACK_PER_DAY_LIMIT = 100


class FeedbackService:
    """Append-only writer for global, content, and quiz-question feedback."""

    def __init__(self, db: Session, scope: UserScope, *, source_mode: str):
        self.db = db
        self.scope = scope
        self.source_mode = source_mode

    def submit(self, body: FeedbackCreate, idempotency_key: str) -> dict:
        request_key = idempotency_key.strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "反馈请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        page_path = self._normalize_page_path(body.page_path)
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "scope": body.scope,
                    "feedbackType": body.feedback_type,
                    "message": body.message,
                    "pagePath": page_path,
                    "view": body.view,
                    "sectionId": body.section_id,
                    "contentVersionId": body.content_version_id,
                    "blockId": body.block_id,
                    **(
                        {
                            "attemptId": body.attempt_id,
                            "questionIndex": body.question_index,
                        }
                        if body.scope == "quiz_question"
                        else {}
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        replay = self._find_replay(request_key)
        if replay:
            return self._replay(replay, request_hash)

        block_snapshot_hash = ""
        quiz_question_context = None
        regeneration = {
            "status": "not_applicable",
            "reasonCode": None,
            "taskId": None,
        }
        if body.scope == "content_block":
            content = self.db.scalar(
                select(ContentVersion)
                .join(Section, Section.id == ContentVersion.section_id)
                .join(Chapter, Chapter.id == Section.chapter_id)
                .join(Book, Book.id == Chapter.book_id)
                .join(Series, Series.id == Book.series_id)
                .join(Shelf, Shelf.id == Series.shelf_id)
                .where(
                    ContentVersion.id == body.content_version_id,
                    ContentVersion.section_id == body.section_id,
                    Shelf.user_id == self.scope.user_id,
                    Shelf.deleted_at.is_(None),
                    Series.deleted_at.is_(None),
                    Book.deleted_at.is_(None),
                )
            )
            if not content:
                raise AppError(
                    "找不到可反馈的正文版本",
                    code="FEEDBACK_TARGET_NOT_FOUND",
                    status=404,
                )
            try:
                blocks = json.loads(content.blocks_json)
            except (TypeError, ValueError):
                blocks = []
            block = next(
                (
                    item for item in blocks
                    if isinstance(item, dict) and str(item.get("id", "")) == body.block_id
                ),
                None,
            )
            if block is None:
                raise AppError(
                    "该段落不属于当前正文版本",
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

            regeneration = self._regeneration_decision(body, content)
        elif body.scope == "quiz_question":
            quiz_question_context = self._quiz_question_context(body)
            regeneration = {
                "status": "needs_review",
                "reasonCode": (
                    "QUIZ_ANSWER_REVIEW_REQUIRED"
                    if body.feedback_type == "inaccurate"
                    else "QUIZ_EXPLANATION_REVIEW_REQUIRED"
                ),
                "taskId": None,
            }

        self._enforce_rate_limit()
        feedback_id = f"feedback_{uuid4().hex}"
        item = UserFeedback(
            id=feedback_id,
            user_id=self.scope.user_id,
            scope=body.scope,
            feedback_type=body.feedback_type,
            message=body.message,
            page_path=page_path,
            view=body.view,
            section_id=body.section_id,
            content_version_id=body.content_version_id,
            block_id=body.block_id,
            block_snapshot_hash=block_snapshot_hash,
            source_mode=self.source_mode,
            schema_version=(
                "feedback_v2"
                if body.scope == "quiz_question"
                else "feedback_v1"
            ),
            idempotency_key=request_key,
            request_hash=request_hash,
            context_json=json.dumps(
                {
                    "pagePath": page_path,
                    "view": body.view,
                    "regeneration": regeneration,
                    **(
                        {"quizQuestion": quiz_question_context}
                        if quiz_question_context
                        else {}
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self.db.add(item)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self._find_replay(request_key)
            if replay:
                return self._replay(replay, request_hash)
            raise
        return self._receipt(item)

    def _quiz_question_context(self, body: FeedbackCreate) -> dict:
        row = self.db.execute(
            select(QuizAttempt, QuizSet)
            .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
            .where(
                QuizAttempt.id == body.attempt_id,
                QuizAttempt.user_id == self.scope.user_id,
                QuizSet.section_id == body.section_id,
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                "找不到可反馈的作答题目",
                code="FEEDBACK_QUIZ_ATTEMPT_NOT_FOUND",
                status=404,
            )
        attempt, quiz = row
        position = int(body.question_index)
        item = self.db.scalar(
            select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == quiz.id,
                AssessmentItemVersion.position == position,
            )
        )
        try:
            questions = json.loads(quiz.questions_json or "[]")
        except (TypeError, ValueError):
            questions = []
        try:
            question = json.loads(item.payload_json) if item else questions[position]
        except (IndexError, TypeError, ValueError):
            question = None
        try:
            answers = json.loads(attempt.answers_json or "[]")
            results = json.loads(attempt.results_json or "[]")
            selected_options = answers[position]
            result = results[position]
        except (IndexError, TypeError, ValueError):
            selected_options = None
            result = None
        if (
            not isinstance(question, dict)
            or not isinstance(selected_options, list)
            or not isinstance(result, dict)
        ):
            raise AppError(
                "这道题的作答记录不完整，暂时无法提交反馈",
                code="FEEDBACK_QUIZ_QUESTION_NOT_FOUND",
                status=404,
            )
        answer = (
            self.db.scalar(
                select(AssessmentAnswerVersion).where(
                    AssessmentAnswerVersion.assessment_item_version_id == item.id
                )
            )
            if item
            else None
        )
        public_snapshot = {
            "prompt": str(question.get("prompt") or ""),
            "options": (
                question.get("options")
                if isinstance(question.get("options"), list)
                else []
            ),
            "objective": str(question.get("objective") or ""),
            "core": bool(question.get("core")),
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(
                public_snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return {
            "sectionId": quiz.section_id,
            "quizSetId": quiz.id,
            "attemptId": attempt.id,
            "questionIndex": position,
            "assessmentItemVersionId": item.id if item else None,
            "assessmentAnswerVersionId": answer.id if answer else None,
            "questionSnapshot": public_snapshot,
            "questionSnapshotHash": snapshot_hash,
            "selectedOptions": selected_options,
            "markedCorrect": bool(result.get("correct")),
            "quizSchemaVersion": quiz.schema_version,
        }

    def _regeneration_decision(
        self,
        body: FeedbackCreate,
        content: ContentVersion,
    ) -> dict:
        # The feedback row remains an immutable observation. The receipt only
        # authorizes the separately tracked repair endpoint when the user is
        # still looking at the latest published lesson version.
        latest_content_id = self.db.scalar(
            select(ContentVersion.id)
            .where(
                ContentVersion.section_id == body.section_id,
                ContentVersion.learning_contract_version_id
                == content.learning_contract_version_id,
                ContentVersion.publication_status == "published",
            )
            .order_by(ContentVersion.version.desc())
        )
        if latest_content_id != content.id:
            return {
                "status": "blocked",
                "reasonCode": "FEEDBACK_CONTENT_VERSION_STALE",
                "taskId": None,
            }
        if body.feedback_type == "inaccurate":
            return {
                "status": "needs_review",
                "reasonCode": "FEEDBACK_ACCURACY_REVIEW_REQUIRED",
                "taskId": None,
            }
        if body.feedback_type == "other":
            return {
                "status": "needs_review",
                "reasonCode": "FEEDBACK_CLASSIFICATION_REQUIRED",
                "taskId": None,
            }
        assessed = self.db.scalar(
            select(func.count())
            .select_from(QuizAttempt)
            .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
            .where(
                QuizAttempt.user_id == self.scope.user_id,
                QuizSet.content_version_id == content.id,
            )
        ) or 0
        if assessed:
            return {
                "status": "recorded_only",
                "reasonCode": "FEEDBACK_ASSESSED_VERSION_FROZEN",
                "taskId": None,
            }
        return {
            "status": "stream_ready",
            "reasonCode": None,
            "taskId": None,
        }

    def _find_replay(self, idempotency_key: str) -> UserFeedback | None:
        return self.db.scalar(
            select(UserFeedback).where(
                UserFeedback.user_id == self.scope.user_id,
                UserFeedback.idempotency_key == idempotency_key,
            )
        )

    def _replay(self, item: UserFeedback, request_hash: str) -> dict:
        if item.request_hash != request_hash:
            raise AppError(
                "反馈请求标识已用于其他内容",
                code="FEEDBACK_IDEMPOTENCY_CONFLICT",
                status=409,
            )
        return self._receipt(item)

    def _enforce_rate_limit(self) -> None:
        observed_at = datetime.now(timezone.utc)
        minute_count = self.db.scalar(
            select(func.count())
            .select_from(UserFeedback)
            .where(
                UserFeedback.user_id == self.scope.user_id,
                UserFeedback.created_at >= observed_at - timedelta(minutes=1),
            )
        ) or 0
        day_count = self.db.scalar(
            select(func.count())
            .select_from(UserFeedback)
            .where(
                UserFeedback.user_id == self.scope.user_id,
                UserFeedback.created_at >= observed_at - timedelta(days=1),
            )
        ) or 0
        if minute_count >= FEEDBACK_PER_MINUTE_LIMIT or day_count >= FEEDBACK_PER_DAY_LIMIT:
            raise AppError(
                "反馈提交得太频繁，请稍后再试",
                code="FEEDBACK_RATE_LIMITED",
                status=429,
                retryable=True,
            )

    @staticmethod
    def _normalize_page_path(value: str) -> str:
        candidate = (value or "/").strip()
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or "\x00" in candidate
        ):
            return "/"
        path = urlsplit(candidate).path or "/"
        return path[:500]

    def _receipt(self, item: UserFeedback) -> dict:
        try:
            context = json.loads(item.context_json or "{}")
        except (TypeError, ValueError):
            context = {}
        regeneration = context.get("regeneration") or {
            "status": "not_applicable",
            "reasonCode": None,
            "taskId": None,
        }
        task = (
            self.db.get(LearningTask, regeneration.get("taskId"))
            if regeneration.get("taskId")
            else None
        )
        return {
            "id": item.id,
            "status": "received",
            "scope": item.scope,
            "createdAt": item.created_at.isoformat(),
            "regeneration": {
                "status": regeneration.get("status", "not_applicable"),
                "reasonCode": regeneration.get("reasonCode"),
                "task": task_view(task) if task else None,
            },
        }
