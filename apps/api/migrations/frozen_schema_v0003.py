"""Immutable ORM metadata snapshot used only by migrations 0001-0003.

Never import application tables here and never update this module for a new
schema. Later schema changes belong in later Alembic revisions.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class FrozenBase(DeclarativeBase):
    pass


class User(FrozenBase):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class Shelf(FrozenBase):
    __tablename__ = "shelves"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(100))
    specialty: Mapped[str] = mapped_column(String(120), default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")


class LearningPlan(FrozenBase):
    __tablename__ = "learning_plans"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    topic: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(80))
    experience: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text, default="")
    depth: Mapped[str] = mapped_column(String(24))
    details: Mapped[str] = mapped_column(Text, default="")
    assumptions_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class PlanCreationRequest(FrozenBase):
    __tablename__ = "plan_creation_requests"
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    series_id: Mapped[str | None] = mapped_column(
        ForeignKey("series.id"),
        nullable=True,
    )
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class Series(FrozenBase):
    __tablename__ = "series"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("learning_plans.id"),
        unique=True,
    )
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    rationale: Mapped[str] = mapped_column(Text)


class Book(FrozenBase):
    __tablename__ = "books"
    __table_args__ = (UniqueConstraint("series_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    topic: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    estimated_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="locked")


class Chapter(FrozenBase):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("book_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="locked")


class Section(FrozenBase):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("chapter_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    question: Mapped[str] = mapped_column(Text)
    objectives_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    best_score: Mapped[int] = mapped_column(Integer, default=0)
    total_score: Mapped[int] = mapped_column(Integer, default=0)
    ask_me_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)


class ContentVersion(FrozenBase):
    __tablename__ = "content_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    blocks_json: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class SourceVerification(FrozenBase):
    __tablename__ = "source_verifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id"),
        unique=True,
    )
    report_json: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class GenerationRun(FrozenBase):
    __tablename__ = "generation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
    )
    operation: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(160), default="")
    trace_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class QuizSet(FrozenBase):
    __tablename__ = "quiz_sets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer)
    questions_json: Mapped[str] = mapped_column(Text)


class QuizAttempt(FrozenBase):
    __tablename__ = "quiz_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    quiz_set_id: Mapped[str] = mapped_column(ForeignKey("quiz_sets.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    answers_json: Mapped[str] = mapped_column(Text)
    results_json: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class Remediation(FrozenBase):
    __tablename__ = "remediations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("quiz_attempts.id"),
        unique=True,
    )
    replacement_quiz_id: Mapped[str] = mapped_column(
        ForeignKey("quiz_sets.id"),
        unique=True,
    )
    blocks_json: Mapped[str] = mapped_column(Text)
    objectives_json: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class QaSession(FrozenBase):
    __tablename__ = "qa_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        unique=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    memory_json: Mapped[str] = mapped_column(Text, default="{}")


class QaMessage(FrozenBase):
    __tablename__ = "qa_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("qa_sessions.id"),
        index=True,
    )
    thread_id: Mapped[str] = mapped_column(String, index=True)
    block_id: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class QaThread(FrozenBase):
    __tablename__ = "qa_threads"
    __table_args__ = (UniqueConstraint("session_id", "thread_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("qa_sessions.id"),
        index=True,
    )
    thread_id: Mapped[str] = mapped_column(String, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(
        String(24),
        default="new_question",
    )
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class LearningNote(FrozenBase):
    __tablename__ = "learning_notes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        unique=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    ai_content_json: Mapped[str] = mapped_column(Text)
    user_content_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class LearningEvidence(FrozenBase):
    __tablename__ = "learning_evidence"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id"),
        index=True,
    )
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
    )
    concept: Mapped[str] = mapped_column(String(300), index=True)
    evidence_type: Mapped[str] = mapped_column(String(32))
    result_json: Mapped[str] = mapped_column(Text)
    mastery_delta: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class LearningMemory(FrozenBase):
    __tablename__ = "learning_memory"
    __table_args__ = (
        UniqueConstraint("user_id", "shelf_id", "concept"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    concept: Mapped[str] = mapped_column(String(300), index=True)
    mastery_score: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class AskMeSession(FrozenBase):
    __tablename__ = "ask_me_sessions"
    __table_args__ = (UniqueConstraint("section_id", "user_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    entries_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class ChapterPractice(FrozenBase):
    __tablename__ = "chapter_practices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id"),
        unique=True,
    )
    title: Mapped[str] = mapped_column(String(240))
    instructions_json: Mapped[str] = mapped_column(Text)
    submission_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class BookCapstone(FrozenBase):
    __tablename__ = "book_capstones"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), unique=True)
    title: Mapped[str] = mapped_column(String(240))
    brief_json: Mapped[str] = mapped_column(Text)
    submission_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class ArtifactAttachment(FrozenBase):
    __tablename__ = "artifact_attachments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(
        String(160),
        default="application/octet-stream",
    )
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(600), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class ChapterRevision(FrozenBase):
    __tablename__ = "chapter_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    action: Mapped[str] = mapped_column(String(24))
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )


class EvaluationRun(FrozenBase):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(24), index=True)
    code_version: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(80), default="v1")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
