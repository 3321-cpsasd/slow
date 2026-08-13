import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.infrastructure.database import build_database
from app.infrastructure.tables import (
    Base,
    Book,
    Chapter,
    ContentVersion,
    LearningPlan,
    LearningRun,
    LearningTask,
    QuizSet,
    Section,
    SectionProgress,
    Series,
    Shelf,
    User,
)
from app.modules.learning.tasks import backfill_missing_lookahead_tasks
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


class NoopAi:
    configured = True
    model = "noop"

    async def close(self):
        return None


@pytest.fixture
def db():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as session:
        yield session
    engine.dispose()


def seed_active_route(db, *, source_status="available", include_next=True):
    db.add(User(id="user_backfill", name="Backfill learner"))
    db.flush()
    db.add(Shelf(
        id="shelf_backfill",
        user_id="user_backfill",
        name="Systems",
        domain="infrastructure",
    ))
    db.flush()
    db.add(LearningPlan(
        id="plan_backfill",
        shelf_id="shelf_backfill",
        topic="Scheduling",
        role="engineer",
        experience="intermediate",
        depth="deep",
        confidence="high",
    ))
    db.flush()
    db.add(Series(
        id="series_backfill",
        plan_id="plan_backfill",
        shelf_id="shelf_backfill",
        title="Scheduling route",
        rationale="Learn scheduling in order",
    ))
    db.flush()
    db.add(Book(
        id="book_backfill",
        series_id="series_backfill",
        shelf_id="shelf_backfill",
        position=1,
        title="Scheduling",
        topic="Scheduling",
        description="Scheduling foundations",
        estimated_minutes=120,
    ))
    db.flush()
    db.add(Chapter(
        id="chapter_backfill",
        book_id="book_backfill",
        position=1,
        title="Priority",
        objective="Explain priority",
    ))
    db.flush()
    db.add(Section(
        id="section_source",
        chapter_id="chapter_backfill",
        position=1,
        title="Current section",
        question="How does priority work?",
        objectives_json='["Explain priority"]',
    ))
    if include_next:
        db.add(Section(
            id="section_target",
            chapter_id="chapter_backfill",
            position=2,
            title="Next section",
            question="What happens next?",
            objectives_json='["Apply priority"]',
        ))
    db.flush()
    db.add(LearningRun(
        id="run_backfill",
        user_id="user_backfill",
        series_id="series_backfill",
        status="active",
    ))
    db.flush()
    db.add(SectionProgress(
        id="progress_source",
        learning_run_id="run_backfill",
        user_id="user_backfill",
        section_id="section_source",
        status=source_status,
    ))
    if include_next:
        db.add(SectionProgress(
            id="progress_target",
            learning_run_id="run_backfill",
            user_id="user_backfill",
            section_id="section_target",
            status="locked",
        ))
    db.flush()
    db.add(ContentVersion(
        id="content_source",
        section_id="section_source",
        version=1,
        blocks_json="[]",
        sources_json="[]",
        confidence="high",
        publication_status="published",
    ))
    db.flush()
    db.add(QuizSet(
        id="quiz_source",
        section_id="section_source",
        content_version_id="content_source",
        generation=1,
        questions_json="[]",
        publication_status="published",
    ))
    db.commit()


def test_backfill_queues_one_idempotent_buffer_without_unlocking_target(db):
    seed_active_route(db)

    assert backfill_missing_lookahead_tasks(db) == 1

    task = db.scalar(select(LearningTask))
    assert task.task_type == "section_lookahead_preload"
    assert task.status == "pending"
    assert task.section_id == "section_source"
    assert task.idempotency_key == "lookahead-after:section_source"
    assert task.trigger_id == "startup-backfill:section_source"
    assert json.loads(task.payload_json) == {"sourceSectionId": "section_source"}
    assert db.get(SectionProgress, "progress_target").status == "locked"

    assert backfill_missing_lookahead_tasks(db) == 0
    assert db.scalar(select(func.count()).select_from(LearningTask)) == 1


def test_backfill_skips_locked_source_and_end_of_route(db):
    seed_active_route(db, source_status="locked")

    assert backfill_missing_lookahead_tasks(db) == 0

    db.get(SectionProgress, "progress_source").status = "available"
    db.delete(db.get(SectionProgress, "progress_target"))
    db.delete(db.get(Section, "section_target"))
    db.commit()

    assert backfill_missing_lookahead_tasks(db) == 0
    assert db.scalar(select(func.count()).select_from(LearningTask)) == 0


def test_backfill_crosses_an_unplanned_chapter_in_the_same_book(db):
    seed_active_route(db, include_next=False)
    db.add(Chapter(
        id="chapter_backfill_next",
        book_id="book_backfill",
        position=2,
        title="QoS",
        objective="Explain QoS",
    ))
    db.commit()

    assert backfill_missing_lookahead_tasks(db) == 1
    task = db.scalar(select(LearningTask))
    assert task.section_id == "section_source"
    assert json.loads(task.payload_json) == {"sourceSectionId": "section_source"}


def test_backfill_stops_at_the_end_of_the_current_book(db):
    seed_active_route(db, include_next=False)
    db.add(Book(
        id="book_backfill_next",
        series_id="series_backfill",
        shelf_id="shelf_backfill",
        position=2,
        title="Advanced scheduling",
        topic="Scheduling",
        description="Advanced scheduling topics",
        estimated_minutes=120,
    ))
    db.flush()
    db.add(Chapter(
        id="chapter_backfill_next_book",
        book_id="book_backfill_next",
        position=1,
        title="Batch scheduling",
        objective="Explain batch scheduling",
    ))
    db.flush()
    db.add(Section(
        id="section_next_book",
        chapter_id="chapter_backfill_next_book",
        position=1,
        title="Queues",
        question="How do queues work?",
        objectives_json='["Explain queues"]',
    ))
    db.commit()

    assert backfill_missing_lookahead_tasks(db) == 0
    assert db.scalar(select(func.count()).select_from(LearningTask)) == 0


def test_api_startup_runs_missing_lookahead_backfill(tmp_path):
    database = tmp_path / "startup-backfill.db"
    database_url = f"sqlite+pysqlite:///{database}"
    engine, sessions = build_database(database_url)
    Base.metadata.create_all(engine)
    with sessions() as db:
        seed_active_route(db)
    engine.dispose()

    app = create_app(
        database_url,
        NoopAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="password",
        app_mode="development",
        registration_mode="closed",
    )
    with TestClient(app):
        with app.state.sessions() as db:
            task = db.scalar(select(LearningTask).where(
                LearningTask.idempotency_key == "lookahead-after:section_source",
            ))
            assert task is not None
            assert task.trigger_id == "startup-backfill:section_source"
