import json
from collections import defaultdict
from fractions import Fraction
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ArtifactProgress,
    ArtifactSubmission,
    Book,
    BookCapstone,
    BookProgress,
    Chapter,
    ChapterPractice,
    ChapterProgress,
    LearningEvidence,
    LearningMemory,
    LearningRun,
    QuizAttempt,
    QuizSet,
    Section,
    SectionProgress,
    User,
    now,
)
from .assessment import rebuild_assessment_projections, section_gate_decision


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _projection(
    db: Session,
    model,
    *,
    learning_run_id: str,
    target_column: str,
    target_id: str,
    user_id: str,
):
    row = db.scalar(
        select(model).where(
            model.learning_run_id == learning_run_id,
            getattr(model, target_column) == target_id,
        )
    )
    if row:
        return row
    row = model(
        id=_uid(model.__tablename__),
        learning_run_id=learning_run_id,
        user_id=user_id,
        **{target_column: target_id},
    )
    db.add(row)
    return row


def rebuild_user_projections(db: Session, *, user_id: str) -> dict:
    """Rebuild all learner projections from immutable attempts and evidence."""

    if not db.get(User, user_id):
        raise AppError(
            "用户不存在",
            code="USER_NOT_FOUND",
            status=404,
        )
    runs = db.scalars(
        select(LearningRun)
        .where(LearningRun.user_id == user_id)
        .order_by(LearningRun.created_at)
    ).all()
    rebuilt = {
        "learningRuns": len(runs),
        "books": 0,
        "chapters": 0,
        "sections": 0,
        "artifacts": 0,
        "memories": 0,
        "assessment": rebuild_assessment_projections(db, user_id=user_id),
    }

    for run in runs:
        books = db.scalars(
            select(Book)
            .where(
                Book.series_id == run.series_id,
                Book.deleted_at.is_(None),
            )
            .order_by(Book.position)
        ).all()
        chapters_by_book = {}
        sections_by_chapter = {}
        all_section_ids = []
        for book in books:
            chapters = db.scalars(
                select(Chapter)
                .where(Chapter.book_id == book.id)
                .order_by(Chapter.position)
            ).all()
            chapters_by_book[book.id] = chapters
            for chapter in chapters:
                sections = db.scalars(
                    select(Section)
                    .where(Section.chapter_id == chapter.id)
                    .order_by(Section.position)
                ).all()
                sections_by_chapter[chapter.id] = sections
                all_section_ids.extend(section.id for section in sections)

        attempts_by_section = defaultdict(list)
        if all_section_ids:
            attempt_rows = db.execute(
                select(QuizAttempt, QuizSet)
                .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
                .where(
                    QuizAttempt.learning_run_id == run.id,
                    QuizAttempt.user_id == user_id,
                    QuizSet.section_id.in_(all_section_ids),
                )
                .order_by(QuizAttempt.created_at)
            ).all()
            for attempt, quiz in attempt_rows:
                results = _load(attempt.results_json, [])
                score = sum(
                    bool(item.get("correct"))
                    for item in results
                )
                attempts_by_section[quiz.section_id].append(
                    {
                        "score": score,
                        "total": len(results),
                        "passed": attempt.passed,
                        "perfect": bool(results) and score == len(results),
                    }
                )

        section_completed = {}
        for section_id, attempts in attempts_by_section.items():
            gate = section_gate_decision(
                db,
                learning_run_id=run.id,
                section_id=section_id,
            )
            section_completed[section_id] = (
                gate.passed
                if gate.fixed_total
                else any(attempt["passed"] for attempt in attempts)
            )
        chapter_completed = {}
        for chapters in chapters_by_book.values():
            for chapter in chapters:
                sections = sections_by_chapter[chapter.id]
                chapter_completed[chapter.id] = bool(sections) and all(
                    section_completed.get(section.id, False)
                    for section in sections
                )
        book_completed = {
            book.id: bool(chapters_by_book[book.id]) and all(
                chapter_completed[chapter.id]
                for chapter in chapters_by_book[book.id]
            )
            for book in books
        }

        previous_books_complete = True
        for book in books:
            outline_confirmed = book.outline_status == "confirmed"
            book_progress = _projection(
                db,
                BookProgress,
                learning_run_id=run.id,
                target_column="book_id",
                target_id=book.id,
                user_id=user_id,
            )
            book_progress.status = (
                "completed"
                if book_completed[book.id]
                else "available"
                if previous_books_complete and outline_confirmed
                else "locked"
            )
            book_progress.updated_at = now()
            rebuilt["books"] += 1

            previous_chapters_complete = (
                previous_books_complete and outline_confirmed
            )
            for chapter in chapters_by_book[book.id]:
                chapter_progress = _projection(
                    db,
                    ChapterProgress,
                    learning_run_id=run.id,
                    target_column="chapter_id",
                    target_id=chapter.id,
                    user_id=user_id,
                )
                chapter_progress.status = (
                    "completed"
                    if chapter_completed[chapter.id]
                    else "available"
                    if previous_chapters_complete
                    else "locked"
                )
                chapter_progress.updated_at = now()
                rebuilt["chapters"] += 1

                previous_sections_complete = previous_chapters_complete
                for section in sections_by_chapter[chapter.id]:
                    section_progress = _projection(
                        db,
                        SectionProgress,
                        learning_run_id=run.id,
                        target_column="section_id",
                        target_id=section.id,
                        user_id=user_id,
                    )
                    attempts = attempts_by_section.get(section.id, [])
                    best = max(
                        attempts,
                        key=lambda item: (
                            Fraction(item["score"], item["total"])
                            if item["total"]
                            else Fraction(-1, 1),
                            item["total"],
                            item["score"],
                        ),
                        default={"score": 0, "total": 0},
                    )
                    completed = section_completed.get(section.id, False)
                    section_progress.status = (
                        "completed"
                        if completed
                        else "available"
                        if previous_sections_complete
                        else "locked"
                    )
                    section_progress.best_score = best["score"]
                    section_progress.total_score = best["total"]
                    section_progress.ask_me_unlocked = any(
                        attempt["perfect"]
                        for attempt in attempts
                    )
                    section_progress.version = (
                        section_progress.version or 0
                    ) + 1
                    section_progress.updated_at = now()
                    rebuilt["sections"] += 1
                    previous_sections_complete &= completed
                previous_chapters_complete &= chapter_completed[chapter.id]
            previous_books_complete &= book_completed[book.id]

        latest_submissions = {}
        for submission in db.scalars(
            select(ArtifactSubmission)
            .where(
                ArtifactSubmission.learning_run_id == run.id,
                ArtifactSubmission.user_id == user_id,
            )
            .order_by(ArtifactSubmission.created_at)
        ).all():
            latest_submissions[
                (submission.target_type, submission.target_id)
            ] = submission

        for book in books:
            for chapter in chapters_by_book[book.id]:
                practice = db.scalar(
                    select(ChapterPractice).where(
                        ChapterPractice.chapter_id == chapter.id
                    )
                )
                if practice:
                    _rebuild_artifact(
                        db,
                        run=run,
                        user_id=user_id,
                        target_type="chapter_practice",
                        target_id=practice.id,
                        available=chapter_completed[chapter.id],
                        submission=latest_submissions.get(
                            ("chapter_practice", practice.id)
                        ),
                    )
                    rebuilt["artifacts"] += 1
            capstone = db.scalar(
                select(BookCapstone).where(
                    BookCapstone.book_id == book.id
                )
            )
            if capstone:
                _rebuild_artifact(
                    db,
                    run=run,
                    user_id=user_id,
                    target_type="book_capstone",
                    target_id=capstone.id,
                    available=book_completed[book.id],
                    submission=latest_submissions.get(
                        ("book_capstone", capstone.id)
                    ),
                )
                rebuilt["artifacts"] += 1

    db.execute(
        delete(LearningMemory).where(
            LearningMemory.user_id == user_id
        )
    )
    memories = {}
    evidence_rows = db.scalars(
        select(LearningEvidence)
        .where(LearningEvidence.user_id == user_id)
        .order_by(LearningEvidence.created_at, LearningEvidence.id)
    ).all()
    m2_attempt_ids = set(
        db.scalars(
            select(QuizAttempt.id).where(
                QuizAttempt.user_id == user_id,
                QuizAttempt.learning_contract_version_id.is_not(None),
            )
        ).all()
    )
    for evidence in evidence_rows:
        evidence_payload = _load(evidence.result_json, {})
        if (
            evidence.evidence_type == "quiz"
            and evidence_payload.get("attemptId") in m2_attempt_ids
        ):
            continue
        key = (evidence.shelf_id, evidence.concept)
        memory = memories.get(key)
        if not memory:
            memory = LearningMemory(
                id=_uid("memory"),
                user_id=user_id,
                shelf_id=evidence.shelf_id,
                concept=evidence.concept,
                mastery_score=0,
                evidence_count=0,
                summary="",
            )
            memories[key] = memory
            db.add(memory)
        memory.mastery_score = max(
            0,
            min(100, memory.mastery_score + evidence.mastery_delta),
        )
        memory.evidence_count += 1
        memory.summary = (
            f"{memory.evidence_count} 条证据，当前掌握度 "
            f"{memory.mastery_score}/100；最近证据："
            f"{evidence.evidence_type}"
        )
        memory.updated_at = evidence.created_at
    rebuilt["memories"] = len(memories)
    db.commit()
    return rebuilt


def _rebuild_artifact(
    db: Session,
    *,
    run: LearningRun,
    user_id: str,
    target_type: str,
    target_id: str,
    available: bool,
    submission: ArtifactSubmission | None,
) -> None:
    row = db.scalar(
        select(ArtifactProgress).where(
            ArtifactProgress.learning_run_id == run.id,
            ArtifactProgress.target_type == target_type,
            ArtifactProgress.target_id == target_id,
        )
    )
    if not row:
        row = ArtifactProgress(
            id=_uid("artifact_progress"),
            learning_run_id=run.id,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
        )
        db.add(row)
    if submission:
        row.status = "completed"
        row.submission_json = json.dumps(
            {
                "content": _load(submission.content_json, {}),
                "attachmentIds": _load(
                    submission.attachment_ids_json,
                    [],
                ),
            },
            ensure_ascii=False,
        )
        row.updated_at = submission.created_at
    else:
        row.status = "available" if available else "locked"
        row.submission_json = "{}"
        row.updated_at = now()
