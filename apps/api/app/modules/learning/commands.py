import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...domain.learning import grade_choice_quiz, passing_score
from ...infrastructure.tables import (
    Book,
    BookCapstone,
    Chapter,
    ChapterPractice,
    GenerationRun,
    LearningContractVersion,
    LearningEvidence,
    LearningMemory,
    LearningTask,
    LearningRunSectionBinding,
    QuizAttempt,
    QuizSet,
    Remediation,
    Section,
    SectionProgress,
    now,
)
from ...platform.unit_of_work import SqlAlchemyUnitOfWork
from ..artifacts.progress import ArtifactProgressStore
from ..library.context import ActiveLearningContextResolver, SectionContext
from .domain import ProgressionDecision, ProgressionPolicy, ProgressionSnapshot
from .assessment import (
    SectionGateDecision,
    record_scoring_facts,
    section_gate_decision,
)
from .assessment_items import immutable_questions_for_quiz
from .content_governance_store import governance_view_for_quiz
from .progress import ProgressStore, best_score_pair
from .contracts import open_run_section
from .decision_snapshots import (
    append_assessment_gate_snapshot,
    append_knowledge_settlement_snapshot,
    append_progression_snapshot,
)
from .knowledge_ranks import knowledge_node_views_for_targets
from .tasks import task_view


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
            next_book_outline_status=(
                next_book.outline_status if next_book else "confirmed"
            ),
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
                self.progress.set_status(section_progress, "preparing")
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

        feedback_regeneration_pending = self.db.scalar(
            select(func.count())
            .select_from(LearningTask)
            .where(
                LearningTask.user_id == self.user_id,
                LearningTask.section_id == section_id,
                LearningTask.task_type == "content_feedback_regeneration",
                LearningTask.status.in_({"pending", "running"}),
            )
        ) or 0
        regeneration_running = self.db.scalar(
            select(func.count())
            .select_from(GenerationRun)
            .where(
                GenerationRun.section_id == section_id,
                GenerationRun.operation.in_({"regeneration", "feedback_repair"}),
                GenerationRun.status == "running",
            )
        ) or 0
        if feedback_regeneration_pending or regeneration_running:
            raise AppError(
                "本节正在根据反馈生成新版本，请完成后再提交验证",
                code="SECTION_REGENERATION_IN_PROGRESS",
                status=409,
            )

        section = context.section
        quiz = self.db.get(QuizSet, body.quiz_set_id)
        if not quiz or quiz.section_id != section_id:
            raise AppError("题集无效", code="QUIZ_INVALID")
        if quiz.publication_status != "published":
            raise AppError(
                "题集不是当前正式发布版本",
                code="QUIZ_NOT_PUBLISHED",
                status=409,
            )
        governance = governance_view_for_quiz(self.db, quiz.id)
        if not governance or not (
            governance["allowed"] and governance["assessmentEligible"]
        ):
            raise AppError(
                "题集未通过正式学习证据治理，不能提交",
                code="QUIZ_GOVERNANCE_REQUIRED",
                status=409,
            )
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )
        if not binding:
            contract = (
                self.db.get(
                    LearningContractVersion,
                    quiz.learning_contract_version_id,
                )
                if quiz.learning_contract_version_id
                else None
            )
            if not contract:
                raise AppError(
                    "旧题集缺少可恢复的学习契约",
                    code="QUIZ_LINEAGE_MISSING",
                    status=409,
                )
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=section,
                mission_version_id=contract.mission_version_id,
                source="quiz_submit_recovery",
                uid=_uid,
                preferred_quiz_id=quiz.id,
            )
        remediation = self.db.scalar(
            select(Remediation).where(Remediation.replacement_quiz_id == quiz.id)
        )
        remediation_allowed = False
        if remediation:
            source_attempt = self.db.get(QuizAttempt, remediation.attempt_id)
            remediation_allowed = bool(
                source_attempt
                and source_attempt.learning_run_id == learning_run.id
                and quiz.learning_contract_version_id
                == binding.learning_contract_version_id
            )
        active_remediation = self.db.scalar(
            select(Remediation)
            .join(QuizAttempt, QuizAttempt.id == Remediation.attempt_id)
            .join(QuizSet, QuizSet.id == Remediation.replacement_quiz_id)
            .where(
                QuizAttempt.learning_run_id == learning_run.id,
                QuizAttempt.user_id == self.user_id,
                QuizSet.learning_contract_version_id
                == binding.learning_contract_version_id,
            )
            .order_by(Remediation.created_at.desc())
        )
        initial_is_superseded = bool(
            quiz.id == binding.initial_quiz_set_id and active_remediation
        )
        if (
            initial_is_superseded
            or (
                quiz.id != binding.initial_quiz_set_id
                and not remediation_allowed
            )
        ):
            raise AppError(
                "题集不属于当前学习实例",
                code="QUIZ_STALE",
                status=409,
            )

        questions = immutable_questions_for_quiz(
            self.db,
            quiz,
            require_versions=bool(remediation),
            require_evidence=True,
        )
        target_ids = {
            str(question.get("assessmentTargetId") or "")
            for question in questions
            if question.get("assessmentTargetId")
        }
        knowledge_before = knowledge_node_views_for_targets(
            self.db,
            user_id=self.user_id,
            target_ids=target_ids,
        )
        grade = grade_choice_quiz(questions, body.answers)
        attempt = QuizAttempt(
            id=_uid("attempt"),
            quiz_set_id=quiz.id,
            learning_contract_version_id=binding.learning_contract_version_id,
            content_version_id=binding.content_version_id,
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
        self.db.flush()
        assistance_mode = (
            "assisted_immediate"
            if remediation
            # A repeated section quiz has no delayed-review assignment or novel
            # item guarantee. Preserve it as practice, never as retention proof.
            else "unassisted_repeat"
            if was_completed
            else "unassisted_initial"
        )
        record_scoring_facts(
            self.db,
            attempt=attempt,
            section=section,
            questions=questions,
            results=grade.results,
            score=grade.score,
            total=grade.total,
            passed=grade.passed,
            assistance_mode=assistance_mode,
            learning_episode_id=(
                f"quiz:{remediation.attempt_id}"
                if remediation
                else f"quiz:{attempt.id}"
            ),
        )
        knowledge_settlement = append_knowledge_settlement_snapshot(
            self.db,
            attempt=attempt,
            section_id=section.id,
            target_ids=target_ids,
            before=knowledge_before,
            trigger_kind="quiz_submit",
        )
        gate_decision = section_gate_decision(
            self.db,
            learning_run_id=learning_run.id,
            section_id=section.id,
        )
        append_assessment_gate_snapshot(
            self.db,
            attempt=attempt,
            section_id=section.id,
            decision=gate_decision,
            trigger_kind="quiz_submit",
        )
        completion_passed = grade.passed if was_completed else gate_decision.passed
        attempt.passed = completion_passed
        best_score, best_total = best_score_pair(
            section_progress.best_score,
            section_progress.total_score,
            grade.score,
            grade.total,
        )
        first_completion = False
        if completion_passed and not was_completed:
            transition = self.db.execute(
                update(SectionProgress)
                .where(
                    SectionProgress.id == section_progress.id,
                    SectionProgress.status != "completed",
                    SectionProgress.version == section_progress.version,
                )
                .values(
                    status="completed",
                    best_score=best_score,
                    total_score=best_total,
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
            section_progress.best_score = best_score
            section_progress.total_score = best_total
            section_progress.ask_me_unlocked |= grade.perfect
            section_progress.version += 1
            section_progress.updated_at = now()
            if completion_passed:
                section_progress.status = "completed"
        if not completion_passed and not was_completed:
            section_progress.status = "available"
        self._record_evidence(
            context,
            questions,
            grade.results,
            attempt.id,
            # Keep the append-only compatibility fact for audit/rebuild, but
            # M2 mastery is projected exclusively from qualified observations.
            # Mirroring it into linear memory would let governance-ineligible
            # evidence re-enter generation context.
            update_legacy_memory=not bool(quiz.learning_contract_version_id),
        )

        workflow_tasks: list[LearningTask] = []
        if first_completion:
            progression_input = self.progression.snapshot(context)
            decision = self.policy.after_quiz_passed(progression_input)
            append_progression_snapshot(
                self.db,
                attempt=attempt,
                section_id=section.id,
                snapshot=progression_input,
                decision=decision,
                trigger_kind="quiz_submit",
            )
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
                    payload_json=_dump({
                        "sourceSectionId": section.id,
                        "targetSectionId": decision.unlocked_section_id,
                    }),
                    status="pending",
                ))
        if not completion_passed and not was_completed:
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
        self.db.flush()
        response = self._response(
            attempt,
            workflow_tasks,
            knowledge_settlement=knowledge_settlement,
        )
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

    def reassess(self, section_id: str, attempt_id: str) -> dict:
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(context.series.id)
        attempt = self.db.scalar(
            select(QuizAttempt)
            .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
            .where(
                QuizAttempt.id == attempt_id,
                QuizAttempt.user_id == self.user_id,
                QuizAttempt.learning_run_id == learning_run.id,
                QuizSet.section_id == section_id,
            )
        )
        if not attempt:
            raise AppError(
                "答题记录不存在",
                code="QUIZ_ATTEMPT_NOT_FOUND",
                status=404,
            )
        results = _load(attempt.results_json, [])
        score = sum(bool(item.get("correct")) for item in results)
        if not passing_score(score, len(results)):
            raise AppError(
                "当前得分仍未达到继续学习标准",
                code="QUIZ_PASS_THRESHOLD_NOT_MET",
                status=409,
            )

        section_progress = self.progress.for_section(
            context.section,
            context.chapter,
            context.book,
        )
        append_assessment_gate_snapshot(
            self.db,
            attempt=attempt,
            section_id=context.section.id,
            decision=SectionGateDecision(
                passed=True,
                initial_score=score,
                adjusted_score=score,
                fixed_total=len(results),
                unresolved_required_target_ids=(),
                unresolved_target_ids=(),
            ),
            trigger_kind="quiz_reassess",
            decision_basis="legacy_score_reassessment",
            rule_version="legacy_score_gate_v1",
        )
        existing_tasks = list(
            self.db.scalars(
                select(LearningTask).where(
                    LearningTask.learning_run_id == learning_run.id,
                    LearningTask.user_id == self.user_id,
                    LearningTask.trigger_id == attempt.id,
                    LearningTask.task_type.in_({
                        "note_generation",
                        "next_section_preload",
                    }),
                )
            ).all()
        )
        if attempt.passed and section_progress.status == "completed":
            response = self._response(attempt, existing_tasks)
            self.uow.commit()
            return response

        best_score, best_total = best_score_pair(
            section_progress.best_score,
            section_progress.total_score,
            score,
            len(results),
        )
        transition = self.db.execute(
            update(SectionProgress)
            .where(
                SectionProgress.id == section_progress.id,
                SectionProgress.status != "completed",
                SectionProgress.version == section_progress.version,
            )
            .values(
                status="completed",
                best_score=best_score,
                total_score=best_total,
                version=section_progress.version + 1,
                updated_at=now(),
            )
            .execution_options(synchronize_session=False)
        )
        first_completion = transition.rowcount == 1
        self.db.expire(section_progress)
        attempt.passed = True
        workflow_tasks = existing_tasks
        if first_completion:
            progression_input = self.progression.snapshot(context)
            decision = self.policy.after_quiz_passed(progression_input)
            append_progression_snapshot(
                self.db,
                attempt=attempt,
                section_id=context.section.id,
                snapshot=progression_input,
                decision=decision,
                trigger_kind="quiz_reassess",
            )
            self.progression.apply(decision)
            self.artifacts.apply_availability(
                learning_run_id=learning_run.id,
                decision=decision,
            )
            note_task = LearningTask(
                id=_uid("task"),
                learning_run_id=learning_run.id,
                section_id=context.section.id,
                user_id=self.user_id,
                task_type="note_generation",
                idempotency_key=f"note:{context.section.id}",
                trigger_id=attempt.id,
                payload_json=_dump({"triggerAttemptId": attempt.id}),
                status="pending",
            )
            workflow_tasks.append(note_task)
            if (
                decision.unlocked_section_id
                or decision.unlocked_chapter_id
                or decision.unlocked_book_id
            ):
                workflow_tasks.append(LearningTask(
                    id=_uid("task"),
                    learning_run_id=learning_run.id,
                    section_id=context.section.id,
                    user_id=self.user_id,
                    task_type="next_section_preload",
                    idempotency_key=f"next-after:{context.section.id}",
                    trigger_id=attempt.id,
                    payload_json=_dump({
                        "sourceSectionId": context.section.id,
                        "targetSectionId": decision.unlocked_section_id,
                    }),
                    status="pending",
                ))
            self.db.add_all(workflow_tasks)

        self.db.flush()

        response = self._response(attempt, workflow_tasks)
        attempt.response_json = _dump(response)
        attempt.workflow_status = "completed"
        attempt.workflow_error_code = ""
        self.uow.commit()
        return response

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
        *,
        update_legacy_memory: bool = True,
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
            if not update_legacy_memory:
                continue
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
        *,
        knowledge_settlement: dict | None = None,
    ) -> dict:
        results = _load(attempt.results_json, [])
        if knowledge_settlement is None and attempt.response_json:
            knowledge_settlement = _load(attempt.response_json, {}).get(
                "knowledgeSettlement"
            )
        score = sum(bool(item.get("correct")) for item in results)
        note_task = next(
            (
                task
                for task in workflow_tasks
                if task.task_type == "note_generation"
            ),
            None,
        )
        task_views = [task_view(task) for task in workflow_tasks]
        return {
            "attemptId": attempt.id,
            "score": score,
            "total": len(results),
            "passed": attempt.passed,
            "perfect": bool(results) and score == len(results),
            "results": results,
            "knowledgeSettlement": knowledge_settlement,
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
