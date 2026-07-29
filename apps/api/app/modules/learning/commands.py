import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...domain.learning import grade_choice_quiz
from ...infrastructure.tables import (
    Book,
    BookCapstone,
    Chapter,
    ChapterPractice,
    LearningEvidence,
    LearningMemory,
    LearningTask,
    QuizAttempt,
    QuizSet,
    Section,
    SectionProgress,
    now,
)
from ...platform.unit_of_work import SqlAlchemyUnitOfWork
from ..artifacts.progress import ArtifactProgressStore
from ..library.context import ActiveLearningContextResolver, SectionContext
from .domain import ProgressionDecision, ProgressionPolicy, ProgressionSnapshot
from .progress import ProgressStore


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


class ProgressionRepository:
    """Builds a rule snapshot and applies, but never decides, progression."""

    def __init__(self, db: Session, progress: ProgressStore):
        self.db = db
        self.progress = progress

    def snapshot(self, context: SectionContext) -> ProgressionSnapshot:
        section, chapter, book = context.section, context.chapter, context.book
        next_section = self.db.scalar(
            select(Section)
            .where(Section.chapter_id == chapter.id, Section.position > section.position)
            .order_by(Section.position)
        )
        practice = self.db.scalar(
            select(ChapterPractice).where(ChapterPractice.chapter_id == chapter.id)
        )
        capstone = self.db.scalar(
            select(BookCapstone).where(BookCapstone.book_id == book.id)
        )
        next_chapter = None
        next_chapter_first_section = None
        next_book = None
        next_book_first_chapter = None
        next_book_first_section = None
        if not next_section:
            next_chapter = self.db.scalar(
                select(Chapter)
                .where(Chapter.book_id == book.id, Chapter.position > chapter.position)
                .order_by(Chapter.position)
            )
            if next_chapter:
                next_chapter_first_section = self.db.scalar(
                    select(Section)
                    .where(Section.chapter_id == next_chapter.id)
                    .order_by(Section.position)
                )
            else:
                next_book = self.db.scalar(
                    select(Book)
                    .where(
                        Book.series_id == book.series_id,
                        Book.position > book.position,
                        Book.deleted_at.is_(None),
                    )
                    .order_by(Book.position)
                )
                if next_book:
                    next_book_first_chapter = self.db.scalar(
                        select(Chapter)
                        .where(Chapter.book_id == next_book.id)
                        .order_by(Chapter.position)
                    )
                    if next_book_first_chapter:
                        next_book_first_section = self.db.scalar(
                            select(Section)
                            .where(Section.chapter_id == next_book_first_chapter.id)
                            .order_by(Section.position)
                        )
        return ProgressionSnapshot(
            section_id=section.id,
            chapter_id=chapter.id,
            book_id=book.id,
            next_section_id=next_section.id if next_section else None,
            next_chapter_id=next_chapter.id if next_chapter else None,
            next_chapter_first_section_id=(
                next_chapter_first_section.id if next_chapter_first_section else None
            ),
            next_book_id=next_book.id if next_book else None,
            next_book_first_chapter_id=(
                next_book_first_chapter.id if next_book_first_chapter else None
            ),
            next_book_first_section_id=(
                next_book_first_section.id if next_book_first_section else None
            ),
            practice_id=practice.id if practice else None,
            capstone_id=capstone.id if capstone else None,
        )

    def apply(self, decision: ProgressionDecision) -> None:
        completed_section = self.db.get(Section, decision.completed_section_id)
        completed_chapter = self.db.get(Chapter, completed_section.chapter_id)
        completed_book = self.db.get(Book, completed_chapter.book_id)
        self.progress.set_status(
            self.progress.for_section(
                completed_section,
                completed_chapter,
                completed_book,
            ),
            "completed",
        )
        if decision.unlocked_section_id:
            section = self.db.get(Section, decision.unlocked_section_id)
            chapter = self.db.get(Chapter, section.chapter_id)
            book = self.db.get(Book, chapter.book_id)
            section_progress = self.progress.for_section(section, chapter, book)
            if section_progress.status == "locked":
                self.progress.set_status(section_progress, "available")
        if decision.completed_chapter_id:
            chapter = self.db.get(Chapter, decision.completed_chapter_id)
            self.progress.set_status(
                self.progress.for_chapter(chapter),
                "completed",
            )
        if decision.unlocked_chapter_id:
            chapter = self.db.get(Chapter, decision.unlocked_chapter_id)
            chapter_progress = self.progress.for_chapter(chapter)
            if chapter_progress.status == "locked":
                self.progress.set_status(chapter_progress, "available")
        if decision.completed_book_id:
            book = self.db.get(Book, decision.completed_book_id)
            self.progress.set_status(self.progress.for_book(book), "completed")
        if decision.unlocked_book_id:
            book = self.db.get(Book, decision.unlocked_book_id)
            book_progress = self.progress.for_book(book)
            if book_progress.status == "locked":
                self.progress.set_status(book_progress, "available")


class SubmitQuiz:
    """One use case owns the core transaction; AI work happens only after commit."""

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
    ):
        self.db = db
        self.user_id = user_id
        self.contexts = ActiveLearningContextResolver(db)
        self.progress = ProgressStore(db, user_id=user_id)
        self.artifacts = ArtifactProgressStore(db, user_id=user_id)
        self.progression = ProgressionRepository(db, self.progress)
        self.policy = ProgressionPolicy()
        self.uow = SqlAlchemyUnitOfWork(db)

    async def execute(self, section_id: str, body, idempotency_key: str | None = None) -> dict:
        request_key = idempotency_key.strip() if idempotency_key else None
        if request_key and not 8 <= len(request_key) <= 128:
            raise AppError(
                "答题请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "sectionId": section_id,
                    "quizSetId": body.quiz_set_id,
                    "answers": body.answers,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        context = self.contexts.resolve_section(user_id=self.user_id, section_id=section_id)
        learning_run = self.progress.active_run(context.series.id)
        replay = self._find_replay(request_key, learning_run.id)
        if replay:
            self._validate_replay(replay, request_hash)
            return self._replay_response(replay)

        section = context.section
        quiz = self.db.get(QuizSet, body.quiz_set_id)
        latest = self.db.scalar(
            select(QuizSet)
            .where(QuizSet.section_id == section_id)
            .order_by(QuizSet.generation.desc())
        )
        if not quiz or quiz.section_id != section_id:
            raise AppError("题集无效", code="QUIZ_INVALID")
        if not latest or latest.id != quiz.id:
            raise AppError("旧题集已失效，请提交当前题集", code="QUIZ_STALE", status=409)

        questions = _load(quiz.questions_json, [])
        grade = grade_choice_quiz(questions, body.answers)
        attempt = QuizAttempt(
            id=_uid("attempt"),
            quiz_set_id=quiz.id,
            learning_run_id=learning_run.id,
            user_id=self.user_id,
            idempotency_key=request_key,
            request_hash=request_hash,
            answers_json=_dump(body.answers),
            results_json=_dump(grade.results),
            passed=grade.passed,
        )
        section_progress = self.progress.for_section(
            section,
            context.chapter,
            context.book,
        )
        was_completed = section_progress.status == "completed"
        self.db.add(attempt)
        first_completion = False
        if grade.passed and not was_completed:
            transition = self.db.execute(
                update(SectionProgress)
                .where(
                    SectionProgress.id == section_progress.id,
                    SectionProgress.status != "completed",
                    SectionProgress.version == section_progress.version,
                )
                .values(
                    status="completed",
                    best_score=max(section_progress.best_score, grade.score),
                    total_score=grade.total,
                    ask_me_unlocked=(
                        section_progress.ask_me_unlocked or grade.perfect
                    ),
                    version=section_progress.version + 1,
                    updated_at=now(),
                )
                .execution_options(synchronize_session=False)
            )
            first_completion = transition.rowcount == 1
            self.db.expire(section_progress)
        else:
            section_progress.best_score = max(section_progress.best_score, grade.score)
            section_progress.total_score = grade.total
            section_progress.ask_me_unlocked |= grade.perfect
            section_progress.version += 1
            section_progress.updated_at = now()
            if grade.passed:
                section_progress.status = "completed"
        if not grade.passed and not was_completed:
            section_progress.status = "available"
        self._record_evidence(context, questions, grade.results, attempt.id)

        workflow_tasks: list[LearningTask] = []
        if first_completion:
            decision = self.policy.after_quiz_passed(self.progression.snapshot(context))
            self.progression.apply(decision)
            self.artifacts.apply_availability(
                learning_run_id=learning_run.id,
                decision=decision,
            )
            workflow_tasks.append(LearningTask(
                id=_uid("task"),
                learning_run_id=learning_run.id,
                section_id=section.id,
                user_id=self.user_id,
                task_type="note_generation",
                idempotency_key=f"note:{section.id}",
                trigger_id=attempt.id,
                payload_json=_dump({"triggerAttemptId": attempt.id}),
                status="pending",
            ))
            if (
                decision.unlocked_section_id
                or decision.unlocked_chapter_id
                or decision.unlocked_book_id
            ):
                workflow_tasks.append(LearningTask(
                    id=_uid("task"),
                    learning_run_id=learning_run.id,
                    section_id=section.id,
                    user_id=self.user_id,
                    task_type="next_section_preload",
                    idempotency_key=f"next-after:{section.id}",
                    trigger_id=attempt.id,
                    payload_json=_dump({"sourceSectionId": section.id}),
                    status="pending",
                ))
        if not grade.passed:
            workflow_tasks.append(LearningTask(
                id=_uid("task"),
                learning_run_id=learning_run.id,
                section_id=section.id,
                user_id=self.user_id,
                task_type="remediation_generation",
                idempotency_key=f"remediation:{attempt.id}",
                trigger_id=attempt.id,
                payload_json=_dump({"attemptId": attempt.id}),
                status="pending",
            ))
        self.db.add_all(workflow_tasks)
        response = self._response(attempt, workflow_tasks)
        attempt.workflow_status = "completed"
        attempt.response_json = _dump(response)
        attempt.workflow_error_code = ""
        try:
            self.uow.commit()
        except IntegrityError:
            self.uow.rollback()
            replay = self._find_replay(request_key, learning_run.id)
            if not replay:
                raise
            self._validate_replay(replay, request_hash)
            return self._replay_response(replay)
        return response

    def _find_replay(
        self,
        request_key: str | None,
        learning_run_id: str,
    ) -> QuizAttempt | None:
        if not request_key:
            return None
        return self.db.scalar(
            select(QuizAttempt).where(
                QuizAttempt.user_id == self.user_id,
                QuizAttempt.learning_run_id == learning_run_id,
                QuizAttempt.idempotency_key == request_key,
            )
        )

    @staticmethod
    def _validate_replay(attempt: QuizAttempt, request_hash: str) -> None:
        if attempt.request_hash != request_hash:
            raise AppError(
                "答题请求标识已用于其他提交",
                code="IDEMPOTENCY_KEY_REUSED",
                status=409,
            )

    def _replay_response(self, attempt: QuizAttempt) -> dict:
        if attempt.workflow_status == "completed" and attempt.response_json:
            return _load(attempt.response_json, {})
        if attempt.workflow_status == "failed":
            raise AppError(
                "答题事实已保存，但后置工作流失败",
                code=attempt.workflow_error_code or "QUIZ_WORKFLOW_FAILED",
                status=502,
            )
        raise AppError(
            "相同答题请求仍在处理中",
            code="QUIZ_SUBMISSION_IN_PROGRESS",
            status=409,
        )

    def _record_evidence(
        self,
        context: SectionContext,
        questions: list[dict],
        results: list[dict],
        attempt_id: str,
    ) -> None:
        for question, result in zip(questions, results, strict=True):
            concept = question["objective"][:300]
            delta = 18 if result["correct"] else -12
            evidence = LearningEvidence(
                id=_uid("evidence"),
                learning_run_id=self.progress.active_run(context.series.id).id,
                user_id=self.user_id,
                shelf_id=context.shelf.id,
                series_id=context.series.id,
                book_id=context.book.id,
                chapter_id=context.chapter.id,
                section_id=context.section.id,
                concept=concept,
                evidence_type="quiz",
                result_json=_dump(
                    {
                        "attemptId": attempt_id,
                        "correct": result["correct"],
                        "core": question.get("core", False),
                    }
                ),
                mastery_delta=delta,
            )
            self.db.add(evidence)
            memory = self.db.scalar(
                select(LearningMemory).where(
                    LearningMemory.user_id == self.user_id,
                    LearningMemory.shelf_id == context.shelf.id,
                    LearningMemory.concept == concept,
                )
            )
            if not memory:
                memory = LearningMemory(
                    id=_uid("memory"),
                    user_id=self.user_id,
                    shelf_id=context.shelf.id,
                    concept=concept,
                    mastery_score=0,
                    evidence_count=0,
                    summary="",
                )
                self.db.add(memory)
            memory.mastery_score = max(0, min(100, memory.mastery_score + delta))
            memory.evidence_count += 1
            memory.summary = (
                f"{memory.evidence_count} 条证据，当前掌握度 "
                f"{memory.mastery_score}/100；最近证据：quiz"
            )
            memory.updated_at = now()

    def _response(
        self,
        attempt: QuizAttempt,
        workflow_tasks: list[LearningTask],
    ) -> dict:
        results = _load(attempt.results_json, [])
        score = sum(bool(item.get("correct")) for item in results)
        note_task = next(
            (
                task
                for task in workflow_tasks
                if task.task_type == "note_generation"
            ),
            None,
        )
        task_views = [
            {
                "taskId": task.id,
                "type": task.task_type,
                "sectionId": task.section_id,
                "status": task.status,
                "retryable": False,
                "errorCode": None,
            }
            for task in workflow_tasks
        ]
        return {
            "attemptId": attempt.id,
            "score": score,
            "total": len(results),
            "passed": attempt.passed,
            "perfect": bool(results) and score == len(results),
            "results": results,
            "remediation": None,
            "nextQuiz": None,
            "workflowTasks": task_views,
            "noteGeneration": (
                {
                    "status": note_task.status,
                    "retryable": False,
                    "errorCode": note_task.error_code or None,
                    "taskId": note_task.id,
                }
                if note_task
                else None
            ),
        }
