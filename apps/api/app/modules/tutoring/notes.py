from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...application.generation_context import GenerationContextBuilder
from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    KnowledgeStateProjection,
    LearningContractVersion,
    LearningMissionVersion,
    LearningNote,
    LearningNoteReviewSupplement,
    LearningNoteSummary,
    LearningNoteUserRevision,
    LearningRunSectionBinding,
    SectionAssessmentTarget,
    now,
)
from ..learning.progress import ProgressStore
from .commands import GenerateLearningNote


class LearningNoteService:
    """Owns learning-note generation, layers, user edits, and review supplements."""

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        tutor,
        contexts,
        progress: ProgressStore,
        generation_contexts: GenerationContextBuilder,
        memory_loader: Callable,
        section_reader: Callable,
        uid: Callable[[str], str],
        dump: Callable,
        load: Callable,
        timestamp: Callable,
    ):
        self.db = db
        self.user_id = user_id
        self.tutor = tutor
        self.contexts = contexts
        self.progress = progress
        self.generation_contexts = generation_contexts
        self.memory_loader = memory_loader
        self.section_reader = section_reader
        self.uid = uid
        self.dump = dump
        self.load = load
        self.timestamp = timestamp

    async def ensure(self, section):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section.id,
        )
        learning_run = self.progress.active_run(context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        contract = (
            self.db.get(
                LearningContractVersion,
                binding.learning_contract_version_id,
            )
            if binding
            else None
        )
        mission = (
            self.db.get(LearningMissionVersion, contract.mission_version_id)
            if contract
            else None
        )
        context_pack = self.generation_contexts.build(
            "learning_note",
            shelf=context.shelf,
            series=context.series,
            book=context.book,
            chapter=context.chapter,
            section=section,
            mission=mission,
            contract=contract,
            memory=self.memory_loader(context.book.shelf_id, 30),
        )
        await GenerateLearningNote(
            self.db,
            user_id=self.user_id,
            learning_run_id=learning_run.id,
            tutor=self.tutor,
            section_reader=self.section_reader,
            generation_context=context_pack.payload(),
        ).execute(section)

    def view(self, note):
        summaries = self.db.scalars(
            select(LearningNoteSummary)
            .where(LearningNoteSummary.note_id == note.id)
            .order_by(LearningNoteSummary.version)
        ).all()
        supplements = self.db.scalars(
            select(LearningNoteReviewSupplement)
            .where(LearningNoteReviewSupplement.note_id == note.id)
            .order_by(
                LearningNoteReviewSupplement.created_at,
                LearningNoteReviewSupplement.id,
            )
        ).all()
        user_revisions = self.db.scalars(
            select(LearningNoteUserRevision)
            .where(LearningNoteUserRevision.note_id == note.id)
            .order_by(LearningNoteUserRevision.version)
        ).all()
        verification_rows = self.db.execute(
            select(KnowledgeStateProjection, AssessmentTarget)
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == KnowledgeStateProjection.assessment_target_id,
            )
            .join(
                SectionAssessmentTarget,
                SectionAssessmentTarget.assessment_target_id
                == AssessmentTarget.id,
            )
            .where(
                KnowledgeStateProjection.user_id == self.user_id,
                SectionAssessmentTarget.section_id == note.section_id,
            )
            .order_by(AssessmentTarget.created_at)
        ).all()
        latest_summary = summaries[-1] if summaries else None
        latest_user = user_revisions[-1] if user_revisions else None
        return {
            "id": note.id,
            "aiContent": self.load(
                latest_summary.content_json
                if latest_summary
                else note.ai_content_json,
                {},
            ),
            "userContent": self.load(
                latest_user.content_json
                if latest_user
                else note.user_content_json,
                {},
            ),
            "version": note.version,
            "layers": {
                "learningSummary": self._summary_view(latest_summary),
                "reviewSupplements": [
                    {
                        "id": item.id,
                        "reviewEpisodeId": item.review_episode_id,
                        "content": self.load(item.content_json, {}),
                        "authorKind": item.author_kind,
                        "sourceObservationWatermark": (
                            item.source_observation_watermark
                        ),
                        "createdAt": self.timestamp(item.created_at),
                    }
                    for item in supplements
                ],
                "userRevision": self._user_revision_view(latest_user),
            },
            "verificationAnnotations": [
                self._verification_view(state, target)
                for state, target in verification_rows
            ],
        }

    def update(self, section_id: str, content: dict):
        note = self._active_note(section_id)
        latest_version = self.db.scalar(
            select(func.max(LearningNoteUserRevision.version)).where(
                LearningNoteUserRevision.note_id == note.id
            )
        ) or 0
        summary_version = self.db.scalar(
            select(func.max(LearningNoteSummary.version)).where(
                LearningNoteSummary.note_id == note.id
            )
        ) or 1
        self.db.add(
            LearningNoteUserRevision(
                id=self.uid("note_user_revision"),
                note_id=note.id,
                version=latest_version + 1,
                content_json=self.dump(content),
                based_on_summary_version=summary_version,
                source="user_edit",
            )
        )
        note.user_content_json = self.dump(content)
        note.version += 1
        note.updated_at = now()
        self.db.commit()
        return self.view(note)

    def add_review_supplement(
        self,
        section_id: str,
        review_episode_id: str,
        content: dict,
    ):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(context.series.id)
        note = self._active_note(section_id, learning_run=learning_run)
        existing = self.db.scalar(
            select(LearningNoteReviewSupplement).where(
                LearningNoteReviewSupplement.note_id == note.id,
                LearningNoteReviewSupplement.review_episode_id
                == review_episode_id,
            )
        )
        if existing:
            self._assert_same_supplement(existing, content)
            return self.view(note)
        watermark = self.db.scalar(
            select(func.max(AssessmentObservation.sequence)).where(
                AssessmentObservation.learning_run_id == learning_run.id,
                AssessmentObservation.user_id == self.user_id,
                AssessmentObservation.section_id == section_id,
                AssessmentObservation.learning_episode_id == review_episode_id,
                AssessmentObservation.assistance_mode == "unassisted_review",
                AssessmentObservation.qualification_at_creation.in_(
                    ("eligible", "eligible_grouped")
                ),
            )
        )
        if not watermark:
            raise AppError(
                "复习补充必须绑定一次已完成的无辅助复习",
                code="NOTE_REVIEW_EPISODE_INVALID",
                status=409,
            )
        self.db.add(
            LearningNoteReviewSupplement(
                id=self.uid("note_review_supplement"),
                note_id=note.id,
                review_episode_id=review_episode_id,
                content_json=self.dump(content),
                author_kind="user",
                source_observation_watermark=watermark,
            )
        )
        note.version += 1
        note.updated_at = now()
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            concurrent = self.db.scalar(
                select(LearningNoteReviewSupplement).where(
                    LearningNoteReviewSupplement.note_id == note.id,
                    LearningNoteReviewSupplement.review_episode_id
                    == review_episode_id,
                )
            )
            if not concurrent:
                raise
            self._assert_same_supplement(concurrent, content)
        return self.view(note)

    def _active_note(self, section_id: str, *, learning_run=None):
        if learning_run is None:
            context = self.contexts.resolve_section(
                user_id=self.user_id,
                section_id=section_id,
            )
            learning_run = self.progress.active_run(context.series.id)
        note = self.db.scalar(
            select(LearningNote).where(
                LearningNote.section_id == section_id,
                LearningNote.user_id == self.user_id,
                LearningNote.learning_run_id == learning_run.id,
            )
        )
        if not note:
            raise AppError("笔记不存在", code="NOTE_NOT_FOUND", status=404)
        return note

    def _assert_same_supplement(self, row, content: dict) -> None:
        if self.load(row.content_json, {}) != content:
            raise AppError(
                "该复习轮次已经形成了不同的笔记补充",
                code="NOTE_REVIEW_EPISODE_REUSED",
                status=409,
            )

    def _summary_view(self, summary):
        if not summary:
            return None
        return {
            "version": summary.version,
            "content": self.load(summary.content_json, {}),
            "sourceContentVersionId": summary.source_content_version_id,
            "sourceContractVersion": summary.source_contract_version,
            "sourceObservationWatermark": summary.source_observation_watermark,
            "generationRuleVersion": summary.generation_rule_version,
            "createdAt": self.timestamp(summary.created_at),
        }

    def _user_revision_view(self, revision):
        if not revision:
            return None
        return {
            "version": revision.version,
            "content": self.load(revision.content_json, {}),
            "basedOnSummaryVersion": revision.based_on_summary_version,
            "source": revision.source,
            "createdAt": self.timestamp(revision.created_at),
        }

    def _verification_view(self, state, target):
        return {
            "assessmentTargetId": target.id,
            "objective": target.objective_statement,
            "dimension": target.dimension,
            "pKnown": round(state.p_known_ppm / 1_000_000, 6),
            "uncertainty": round(state.uncertainty_ppm / 1_000_000, 6),
            "claimStatus": state.claim_status,
            "retentionRounds": state.retention_rounds,
            "parameterSetVersion": state.parameter_set_version,
            "projectionRuleVersion": state.projection_rule_version,
            "sourceObservationWatermark": state.source_observation_watermark,
        }
