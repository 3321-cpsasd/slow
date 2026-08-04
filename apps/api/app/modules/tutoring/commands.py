import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...infrastructure.tables import (
    AssessmentObservation,
    LearningNote,
    LearningNoteSummary,
    QaMessage,
    QaSession,
    QuizAttempt,
    QuizSet,
    Section,
)
from ...platform.unit_of_work import SqlAlchemyUnitOfWork


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


class GenerateLearningNote:
    """Prepares context, closes the read transaction, then calls the AI port."""

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        learning_run_id: str,
        tutor,
        section_reader: Callable[[str], dict],
    ):
        self.db = db
        self.user_id = user_id
        self.learning_run_id = learning_run_id
        self.tutor = tutor
        self.section_reader = section_reader
        self.uow = SqlAlchemyUnitOfWork(db)

    async def execute(self, section: Section) -> None:
        existing = self.db.scalar(
            select(LearningNote).where(
                LearningNote.section_id == section.id,
                LearningNote.user_id == self.user_id,
                LearningNote.learning_run_id == self.learning_run_id,
            )
        )
        if existing:
            summary = self.db.scalar(
                select(LearningNoteSummary).where(
                    LearningNoteSummary.note_id == existing.id
                )
            )
            if not summary:
                self.db.add(
                    LearningNoteSummary(
                        id=_uid("note_summary"),
                        note_id=existing.id,
                        version=1,
                        content_json=existing.ai_content_json,
                        source_contract_version="legacy_learning_note_v1",
                        source_observation_watermark=0,
                        generation_rule_version="legacy_note_import_v1",
                    )
                )
            self.uow.commit()
            return
        view = self.section_reader(section.id)
        messages = self.db.scalars(
            select(QaMessage)
            .join(QaSession, QaSession.id == QaMessage.session_id)
            .where(
                QaSession.section_id == section.id,
                QaSession.user_id == self.user_id,
                QaSession.learning_run_id == self.learning_run_id,
            )
            .order_by(QaMessage.created_at)
        ).all()
        attempts = self.db.scalars(
            select(QuizAttempt)
            .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
            .where(
                QuizSet.section_id == section.id,
                QuizAttempt.user_id == self.user_id,
                QuizAttempt.learning_run_id == self.learning_run_id,
            )
            .order_by(QuizAttempt.created_at)
        ).all()
        quiz_evidence = [_load(item.results_json, []) for item in attempts]
        source_observation_watermark = self.db.scalar(
            select(func.max(AssessmentObservation.sequence)).where(
                AssessmentObservation.learning_run_id == self.learning_run_id,
                AssessmentObservation.user_id == self.user_id,
                AssessmentObservation.section_id == section.id,
            )
        ) or 0
        wrong_concepts = list(dict.fromkeys(
            str(result.get("objective", "")).strip()
            for evidence in quiz_evidence
            for result in evidence
            if not result.get("correct") and str(result.get("objective", "")).strip()
        ))
        request = {
            "section": view,
            "qa": [
                {
                    "role": item.role,
                    "content": item.content,
                    "threadId": item.thread_id,
                }
                for item in messages
            ],
            "quizEvidence": quiz_evidence,
            "wrongConcepts": wrong_concepts,
        }
        # SELECTs autobegin in SQLAlchemy. End that transaction before network I/O.
        self.uow.commit()
        generated = await self.tutor.note(request)
        content = generated.model_dump()
        generated_gaps = [
            str(item).strip()
            for item in content.get("personal_gaps", [])
            if str(item).strip()
        ]
        content["personal_gaps"] = list(dict.fromkeys([
            *wrong_concepts,
            *generated_gaps,
        ]))
        note = LearningNote(
            id=_uid("note"),
            learning_run_id=self.learning_run_id,
            section_id=section.id,
            user_id=self.user_id,
            ai_content_json=_dump(content),
            user_content_json="{}",
        )
        self.db.add(note)
        self.db.flush()
        content_version_id = (
            view.get("content", {}).get("id") if view.get("content") else None
        )
        self.db.add(
            LearningNoteSummary(
                id=_uid("note_summary"),
                note_id=note.id,
                version=1,
                content_json=_dump(content),
                source_content_version_id=content_version_id,
                source_contract_version="generated_note_v1",
                source_observation_watermark=source_observation_watermark,
                generation_rule_version="note_summary_v2",
            )
        )
