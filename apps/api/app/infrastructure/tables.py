from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class Shelf(Base):
    __tablename__ = "shelves"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(100))
    specialty: Mapped[str] = mapped_column(String(120), default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")


class LearningPlan(Base):
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlanCreationRequest(Base):
    __tablename__ = "plan_creation_requests"
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    series_id: Mapped[str | None] = mapped_column(ForeignKey("series.id"), nullable=True)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Series(Base):
    __tablename__ = "series"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("learning_plans.id"), unique=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    rationale: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Book(Base):
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("book_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="locked")


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("chapter_id", "position"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    question: Mapped[str] = mapped_column(Text)
    objectives_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    best_score: Mapped[int] = mapped_column(Integer, default=0)
    total_score: Mapped[int] = mapped_column(Integer, default=0)
    ask_me_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)


class LearningRun(Base):
    __tablename__ = "learning_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index(
            "uq_learning_runs_active_user_series",
            "user_id",
            "series_id",
            unique=True,
            sqlite_where=(status == "active"),
        ),
    )


class BookProgress(Base):
    __tablename__ = "book_progress"
    __table_args__ = (UniqueConstraint("learning_run_id", "book_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChapterProgress(Base):
    __tablename__ = "chapter_progress"
    __table_args__ = (UniqueConstraint("learning_run_id", "chapter_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SectionProgress(Base):
    __tablename__ = "section_progress"
    __table_args__ = (UniqueConstraint("learning_run_id", "section_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    best_score: Mapped[int] = mapped_column(Integer, default=0)
    total_score: Mapped[int] = mapped_column(Integer, default=0)
    ask_me_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentVersion(Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint(
            "section_id",
            "version",
            name="uq_content_versions_section_version",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    blocks_json: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceVerification(Base):
    __tablename__ = "source_verifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_version_id: Mapped[str] = mapped_column(ForeignKey("content_versions.id"), unique=True)
    report_json: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GenerationRun(Base):
    __tablename__ = "generation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(160), default="")
    trace_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuizSet(Base):
    __tablename__ = "quiz_sets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    content_version_id: Mapped[str] = mapped_column(ForeignKey("content_versions.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    questions_json: Mapped[str] = mapped_column(Text)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "user_id",
            "idempotency_key",
            name="uq_quiz_attempts_run_user_idempotency",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    quiz_set_id: Mapped[str] = mapped_column(ForeignKey("quiz_sets.id"))
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    answers_json: Mapped[str] = mapped_column(Text)
    results_json: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool] = mapped_column(Boolean)
    workflow_status: Mapped[str] = mapped_column(String(24), default="processing")
    response_json: Mapped[str] = mapped_column(Text, default="")
    workflow_error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Remediation(Base):
    __tablename__ = "remediations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("quiz_attempts.id"), unique=True)
    replacement_quiz_id: Mapped[str] = mapped_column(ForeignKey("quiz_sets.id"), unique=True)
    blocks_json: Mapped[str] = mapped_column(Text)
    objectives_json: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QaSession(Base):
    __tablename__ = "qa_sessions"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_qa_sessions_run_section_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    memory_json: Mapped[str] = mapped_column(Text, default="{}")


class QaMessage(Base):
    __tablename__ = "qa_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("qa_sessions.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String, index=True)
    block_id: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QaThread(Base):
    __tablename__ = "qa_threads"
    __table_args__ = (UniqueConstraint("session_id", "thread_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("qa_sessions.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(24), default="new_question")
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningNote(Base):
    __tablename__ = "learning_notes"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_learning_notes_run_section_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    ai_content_json: Mapped[str] = mapped_column(Text)
    user_content_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningTask(Base):
    """Durable AI work created by a committed learning fact.

    The task row is orchestration state, never the authority for quiz results,
    progression, generated content, or learning evidence.
    """

    __tablename__ = "learning_tasks"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "task_type",
            "idempotency_key",
            name="uq_learning_tasks_run_type_idempotency",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(
        ForeignKey("learning_runs.id"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    trigger_id: Mapped[str] = mapped_column(String(160), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), index=True, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningEvidence(Base):
    __tablename__ = "learning_evidence"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("series.id"), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    concept: Mapped[str] = mapped_column(String(300), index=True)
    evidence_type: Mapped[str] = mapped_column(String(32))
    result_json: Mapped[str] = mapped_column(Text)
    mastery_delta: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LearningMemory(Base):
    __tablename__ = "learning_memory"
    __table_args__ = (UniqueConstraint("user_id", "shelf_id", "concept"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(ForeignKey("shelves.id"), index=True)
    concept: Mapped[str] = mapped_column(String(300), index=True)
    mastery_score: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AskMeSession(Base):
    __tablename__ = "ask_me_sessions"
    __table_args__ = (
        UniqueConstraint(
            "learning_run_id",
            "section_id",
            "user_id",
            name="uq_ask_me_sessions_run_section_user",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("sections.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    entries_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChapterPractice(Base):
    __tablename__ = "chapter_practices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id"), unique=True)
    title: Mapped[str] = mapped_column(String(240))
    instructions_json: Mapped[str] = mapped_column(Text)
    submission_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BookCapstone(Base):
    __tablename__ = "book_capstones"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), unique=True)
    title: Mapped[str] = mapped_column(String(240))
    brief_json: Mapped[str] = mapped_column(Text)
    submission_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="locked")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactProgress(Base):
    __tablename__ = "artifact_progress"
    __table_args__ = (
        UniqueConstraint("learning_run_id", "target_type", "target_id"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String(24), default="locked")
    submission_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactAttachment(Base):
    __tablename__ = "artifact_attachments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    learning_run_id: Mapped[str] = mapped_column(ForeignKey("learning_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(600), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChapterRevision(Base):
    __tablename__ = "chapter_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    action: Mapped[str] = mapped_column(String(24))
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(24), index=True)
    code_version: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(80), default="v1")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
