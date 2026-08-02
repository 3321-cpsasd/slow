import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...infrastructure.tables import (
    LearningNote,
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
        self.db.add(
            LearningNote(
                id=_uid("note"),
                learning_run_id=self.learning_run_id,
                section_id=section.id,
                user_id=self.user_id,
                ai_content_json=_dump(content),
                user_content_json="{}",
            )
        )
