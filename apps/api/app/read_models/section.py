import json
from collections.abc import Callable
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..domain.learning import passing_score
from ..infrastructure.tables import (
    ContentVersion,
    GenerationRun,
    GovernanceDecisionSnapshot,
    LearningNote,
    LearningRunSectionBinding,
    LearningTask,
    PersonalBlockPresentation,
    QuizAttempt,
    QuizSet,
    Remediation,
    SourceVerification,
    now,
)
from ..modules.learning.content_governance_store import governance_view_for_quiz
from ..modules.learning.assessment_items import immutable_questions_for_quiz
from ..modules.learning.tasks import task_view


def _load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _timestamp(value):
    return value.isoformat() if value else None


class SectionReadModel:
    """Build the authoritative learner-facing view for one section.

    This reader never repairs projections or publishes content. It only exposes
    versions already accepted by the write-side generation and learning rules.
    """

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        contexts,
        progress,
        note_reader: Callable[[LearningNote], dict],
    ):
        self.db = db
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress
        self.note_reader = note_reader

    def get(self, section_id: str, *, allow_preparing: bool = False) -> dict:
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = context.section
        section_progress = self.progress.for_section(
            section,
            context.chapter,
            context.book,
        )
        if section_progress.status == "locked" and not allow_preparing:
            raise AppError("小节未解锁", code="SECTION_LOCKED", status=403)
        if section_progress.status == "preparing" and not allow_preparing:
            raise AppError(
                "下一节正文和验证题仍在准备中",
                code="SECTION_PREPARING",
                status=409,
            )
        learning_run = self.progress.active_run(context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        content = self._published_content(section.id, binding)
        quiz = self._published_quiz(section.id, binding)
        note = self.db.scalar(
            select(LearningNote).where(
                LearningNote.section_id == section.id,
                LearningNote.user_id == self.user_id,
                LearningNote.learning_run_id == learning_run.id,
            )
        )
        generation = self.db.scalar(
            select(GenerationRun)
            .where(GenerationRun.section_id == section.id)
            .order_by(GenerationRun.started_at.desc())
        )
        remediations = self._remediations(section.id, learning_run.id)
        if binding and remediations:
            bound = next(
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
            if bound:
                quiz = self.db.get(QuizSet, bound.replacement_quiz_id)

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
            if (trace := _load(item.trace_json, {})).get("quizSetId")
        }
        workflow_tasks = self.db.scalars(
            select(LearningTask)
            .where(
                LearningTask.learning_run_id == learning_run.id,
                LearningTask.user_id == self.user_id,
                LearningTask.section_id == section.id,
                LearningTask.task_type != "section_lookahead_preload",
            )
            .order_by(LearningTask.created_at)
        ).all()
        verification = (
            self.db.scalar(
                select(SourceVerification).where(
                    SourceVerification.content_version_id == content.id
                )
            )
            if content
            else None
        )
        boundary_validation = self._boundary_validation(content)
        governance = governance_view_for_quiz(
            self.db,
            quiz.id if quiz else None,
        )
        quiz_is_eligible = bool(
            governance
            and governance["allowed"]
            and governance["assessmentEligible"]
        )
        public_questions = self._public_questions(
            self._quiz_questions(quiz) if quiz and quiz_is_eligible else []
        )
        latest_attempt = (
            self.db.scalar(
                select(QuizAttempt)
                .where(
                    QuizAttempt.learning_run_id == learning_run.id,
                    QuizAttempt.user_id == self.user_id,
                    QuizAttempt.quiz_set_id == quiz.id,
                )
                .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            )
            if quiz
            else None
        )
        latest_results = (
            _load(latest_attempt.results_json, []) if latest_attempt else []
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
            **self._summary(section, section_progress),
            "versionBinding": self._binding_view(binding),
            "generation": self._generation_view(generation),
            "content": (
                {
                    "id": content.id,
                    "version": content.version,
                    "blocks": self._blocks_with_personal_presentations(content),
                    "sources": _load(content.sources_json, []),
                    "sourceVerification": (
                        _load(verification.report_json, []) if verification else []
                    ),
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
                else None
            ),
            "quiz": (
                {
                    "id": quiz.id,
                    "generation": quiz.generation,
                    "publicationStatus": quiz.publication_status,
                    "questions": public_questions,
                    "governance": governance,
                }
                if quiz
                else None
            ),
            "latestAttemptReview": (
                {
                    "attemptId": latest_attempt.id,
                    "score": sum(
                        bool(item.get("correct")) for item in latest_results
                    ),
                    "total": len(latest_results),
                    "passed": latest_attempt.passed,
                    "reassessmentEligible": (
                        not latest_attempt.passed
                        and passing_score(
                            sum(
                                bool(item.get("correct"))
                                for item in latest_results
                            ),
                            len(latest_results),
                        )
                    ),
                    "perfect": bool(latest_results)
                    and all(item.get("correct") for item in latest_results),
                    "results": latest_results,
                    "questions": self._public_questions(
                        self._quiz_questions(latest_attempt_quiz)
                    )
                    if latest_attempt_quiz
                    else [],
                    "remediation": None,
                    "nextQuiz": None,
                    "workflowTasks": latest_attempt_tasks,
                    "noteGeneration": None,
                }
                if latest_attempt
                else None
            ),
            "remediations": [
                self._remediation_view(item, remediation_run_by_quiz)
                for item in remediations
            ],
            "note": self.note_reader(note) if note else None,
            "workflowTasks": [task_view(task) for task in workflow_tasks],
        }

    def _published_content(self, section_id, binding):
        content = (
            self.db.get(ContentVersion, binding.content_version_id)
            if binding
            else self.db.scalar(
                select(ContentVersion)
                .where(
                    ContentVersion.section_id == section_id,
                    ContentVersion.publication_status == "published",
                )
                .order_by(ContentVersion.version.desc())
            )
        )
        return content if not content or content.publication_status == "published" else None

    def _blocks_with_personal_presentations(self, content) -> list[dict]:
        blocks = _load(content.blocks_json, []) or []
        overrides = {
            item.block_id: item
            for item in self.db.scalars(
                select(PersonalBlockPresentation).where(
                    PersonalBlockPresentation.user_id == self.user_id,
                    PersonalBlockPresentation.content_version_id == content.id,
                    PersonalBlockPresentation.active.is_(True),
                )
            )
        }
        return [
            {
                **block,
                **(
                    {"personalPresentation": {
                        "id": overrides[block.get("id")].id,
                        "content": overrides[block.get("id")].replacement_content,
                        "source": "ask_ai",
                        "updatedAt": overrides[block.get("id")].updated_at.isoformat(),
                    }}
                    if block.get("id") in overrides
                    else {}
                ),
            }
            for block in blocks
        ]

    def _published_quiz(self, section_id, binding):
        quiz = (
            self.db.get(QuizSet, binding.initial_quiz_set_id)
            if binding and binding.initial_quiz_set_id
            else self.db.scalar(
                select(QuizSet)
                .where(
                    QuizSet.section_id == section_id,
                    QuizSet.publication_status == "published",
                )
                .order_by(QuizSet.generation.desc())
            )
        )
        return quiz if not quiz or quiz.publication_status == "published" else None

    def _remediations(self, section_id, learning_run_id):
        revisions = self.db.scalars(
            select(Remediation)
            .join(QuizAttempt, QuizAttempt.id == Remediation.attempt_id)
            .where(
                Remediation.section_id == section_id,
                QuizAttempt.learning_run_id == learning_run_id,
            )
            .order_by(Remediation.created_at)
        ).all()
        return list({item.attempt_id: item for item in revisions}.values())

    def _quiz_questions(self, quiz: QuizSet) -> list[dict]:
        return immutable_questions_for_quiz(
            self.db,
            quiz,
            require_evidence=quiz.schema_version != "legacy",
        )

    def _boundary_validation(self, content):
        if not content:
            return {"status": "unverified", "ruleVersion": None}
        snapshot = self.db.scalar(
            select(GovernanceDecisionSnapshot)
            .where(
                GovernanceDecisionSnapshot.content_version_id == content.id,
                GovernanceDecisionSnapshot.decision_scope == "content_publication",
                GovernanceDecisionSnapshot.quiz_set_id.is_(None),
            )
            .order_by(GovernanceDecisionSnapshot.created_at.desc())
        )
        origin = (
            self.db.get(GenerationRun, content.generation_run_id)
            if content.generation_run_id
            else None
        )
        metadata = _load(content.labeling_metadata_json, {}) or {}
        passed = bool(
            content.schema_version != "legacy"
            and content.prompt_version != "legacy"
            and origin
            and origin.status == "succeeded"
            and origin.id == content.generation_run_id
            and origin.schema_version == content.schema_version
            and origin.prompt_version == content.prompt_version
            and snapshot
            and snapshot.allowed
            and snapshot.assessment_eligible
            and snapshot.mode == "contract_boundary"
            and snapshot.learning_contract_version_id
            == content.learning_contract_version_id
            and snapshot.actor_kind == "generation_attempt"
            and snapshot.actor_id == origin.id
            and metadata.get("schemaVersionOfCandidate") == content.schema_version
            and metadata.get("promptVersion") == content.prompt_version
            and metadata.get("ruleVersion") == snapshot.rule_version
        )
        return {
            "status": (
                "passed"
                if passed
                else "legacy"
                if content.schema_version == "legacy"
                else "unverified"
            ),
            "ruleVersion": snapshot.rule_version if snapshot else None,
        }

    @staticmethod
    def _public_questions(items):
        return [
            {
                **{
                    key: value
                    for key, value in question.items()
                    if key
                    not in {"correct", "explanation", "claim_block_indexes"}
                },
                "selectionMode": (
                    "multiple"
                    if len(set(question.get("correct", []))) > 1
                    else "single"
                ),
            }
            for question in items
        ]

    @staticmethod
    def _summary(section, progress):
        return {
            "id": section.id,
            "position": section.position,
            "title": section.title,
            "question": section.question,
            "objectives": _load(section.objectives_json, []),
            "status": progress.status,
            "bestScore": progress.best_score,
            "totalScore": progress.total_score,
            "askMeUnlocked": progress.ask_me_unlocked,
        }

    @staticmethod
    def _binding_view(binding):
        if not binding:
            return None
        return {
            "id": binding.id,
            "learningContractVersionId": binding.learning_contract_version_id,
            "contentVersionId": binding.content_version_id,
            "initialQuizSetId": binding.initial_quiz_set_id,
            "firstReadAt": _timestamp(binding.first_read_at),
            "source": binding.source,
        }

    @staticmethod
    def _generation_view(run):
        if not run:
            return None
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
            "trace": _load(run.trace_json, {}),
            "errorCode": run.error_code or None,
            "error": run.error_message or None,
            "startedAt": _timestamp(run.started_at),
            "finishedAt": _timestamp(run.finished_at),
            "durationMs": max(
                0,
                int((finished - started).total_seconds() * 1000),
            ),
        }

    @staticmethod
    def _remediation_view(item, runs):
        run = runs.get(item.replacement_quiz_id)
        return {
            "id": item.id,
            "attemptId": item.attempt_id,
            "replacementQuizId": item.replacement_quiz_id,
            "blocks": _load(item.blocks_json, []),
            "objectives": _load(item.objectives_json, []),
            "strategy": item.strategy,
            "sourceVerification": run[1].get("sourceVerification", []) if run else [],
            "sourceLineage": (
                {"mode": "generation_trace", "generationRunId": run[0].id}
                if run
                else {"mode": "missing", "generationRunId": None}
            ),
        }
