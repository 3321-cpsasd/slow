"""Auditable synthetic-learner study primitives for the GOAI evidence pack.

The study is deliberately separate from normal product behavior.  A frozen
real-provider database snapshot is cloned for every episode, deterministic
setup replays the already-published curriculum up to the target section, and
only the provider adapter receives the FULL/NO_MEMORY manipulation.  No row in
the source snapshot or production database is modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ..ai.local_adapter import LocalDemoAdapter
from ..application.service import DEMO_USER_ID
from ..infrastructure.database import build_database
from ..infrastructure.tables import (
    Book,
    Chapter,
    AssessmentObservation,
    KnowledgeStateProjection,
    LearningRun,
    QuizAttempt,
    QuizSet,
    Remediation,
    ScoringResult,
    Section,
    SectionProgress,
    Shelf,
    UserProfile,
)
from ..main import create_app
from ..modules.learning.rebuild import rebuild_user_projections


StudyCondition = Literal["FULL", "NO_MEMORY"]
ABLATION_TRANSFORM_VERSION = "relevant_memory_ablation_v1"
FIXTURE_SETUP_VERSION = "synthetic_episode_setup_v1"


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return f"<external>/{path.name}"


def migrate_episode_database(database_path: Path, workspace: Path) -> None:
    """Upgrade only the disposable episode clone to the current schema."""
    # M2 acceptance snapshots were produced from SQLAlchemy metadata and do not
    # carry an Alembic stamp.  They already contain the 0043 schema; the only
    # later additive change is the 0044 chapter scope column.  Stamp that exact
    # compatibility upgrade instead of replaying legacy migrations whose input
    # columns intentionally no longer exist in metadata-created snapshots.
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "alembic_version" not in tables:
            chapter_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(chapters)")
            }
            if "knowledge_identity_scope_json" not in chapter_columns:
                connection.execute(
                    "ALTER TABLE chapters ADD COLUMN "
                    "knowledge_identity_scope_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.execute(
                "INSERT INTO alembic_version(version_num) VALUES (?)",
                ("0044_chapter_knowledge_identity_scope",),
            )
            connection.commit()
            return

    api_root = workspace / "apps" / "api"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        "PYTHONPATH": ".",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=api_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "episode database migration failed: "
            + completed.stderr[-1200:].replace(str(workspace), "<workspace>")
        )


class SyntheticAblationAdapter:
    """Provider wrapper that removes only qualified longitudinal memory.

    The wrapper records both the pre-transform and delivered hashes.  The
    current attempt, learner, mission, contract, curriculum, knowledge graph,
    feedback, and all other provider inputs remain byte-for-byte equivalent
    after canonical JSON serialization.
    """

    def __init__(self, delegate, condition: StudyCondition):
        if condition not in {"FULL", "NO_MEMORY"}:
            raise ValueError(f"unsupported study condition: {condition}")
        self.delegate = delegate
        self.condition = condition
        self.context_audit: list[dict[str, Any]] = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    @property
    def input_tokens(self):
        return getattr(self.delegate, "input_tokens", 0)

    @property
    def output_tokens(self):
        return getattr(self.delegate, "output_tokens", 0)

    def structured_trace(self):
        method = getattr(self.delegate, "structured_trace", None)
        return method() if callable(method) else []

    @staticmethod
    def _memory_ids(memory: list[dict[str, Any]]) -> list[str]:
        return sorted(
            {
                str(
                    item.get("assessmentTargetId")
                    or item.get("conceptKey")
                    or item.get("concept")
                    or stable_hash(item)[:16]
                )
                for item in memory
            }
        )

    def _prepare(
        self,
        operation: str,
        request: dict[str, Any],
        memory: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        delivered_request = deepcopy(request)
        delivered_memory = deepcopy(memory) if memory is not None else None
        generation_context = delivered_request.get("generationContext")
        embedded_memory: list[dict[str, Any]] = []
        if isinstance(generation_context, dict):
            learning_state = generation_context.get("learningState")
            if isinstance(learning_state, dict):
                candidate = learning_state.get("relevantMemory")
                if isinstance(candidate, list):
                    embedded_memory = deepcopy(candidate)
                    if self.condition == "NO_MEMORY":
                        learning_state["relevantMemory"] = []
        supplied_memory = deepcopy(memory or [])
        if self.condition == "NO_MEMORY" and delivered_memory is not None:
            delivered_memory = []

        pre = {"request": request, "memory": memory}
        delivered = {
            "request": delivered_request,
            "memory": delivered_memory,
        }
        pre_memory = embedded_memory or supplied_memory
        delivered_embedded = (
            delivered_request.get("generationContext", {})
            .get("learningState", {})
            .get("relevantMemory", [])
        )
        delivered_effective = delivered_embedded or (delivered_memory or [])
        self.context_audit.append(
            {
                "operation": operation,
                "condition": self.condition,
                "transformVersion": ABLATION_TRANSFORM_VERSION,
                "preTransformHash": stable_hash(pre),
                "deliveredHash": stable_hash(delivered),
                "preMemoryCount": len(pre_memory),
                "deliveredMemoryCount": len(delivered_effective),
                "removedEvidenceIds": (
                    self._memory_ids(pre_memory)
                    if self.condition == "NO_MEMORY"
                    else []
                ),
            }
        )
        return delivered_request, delivered_memory

    async def close(self):
        close = getattr(self.delegate, "close", None)
        if close:
            return await close()
        return None

    async def plan(self, request, memory):
        request, memory = self._prepare("plan", request, memory)
        return await self.delegate.plan(request, memory)

    async def chapter(self, request, memory):
        request, memory = self._prepare("chapter", request, memory)
        return await self.delegate.chapter(request, memory)

    async def teaching_blueprint(self, request, memory):
        request, memory = self._prepare("teaching_blueprint", request, memory)
        return await self.delegate.teaching_blueprint(request, memory)

    async def generate_lesson(self, spec):
        spec, _ = self._prepare("generate_lesson", spec)
        return await self.delegate.generate_lesson(spec)

    async def lesson_content(self, request, memory, prior_questions=None):
        request, memory = self._prepare("lesson_content", request, memory)
        return await self.delegate.lesson_content(request, memory, prior_questions)

    async def repair_lesson_sources(
        self,
        request,
        memory,
        content,
        failed_sources,
        prior_questions=None,
    ):
        request, memory = self._prepare("repair_lesson_sources", request, memory)
        return await self.delegate.repair_lesson_sources(
            request,
            memory,
            content,
            failed_sources,
            prior_questions,
        )

    async def lesson_quiz(self, request, content, prior_questions=None):
        request, _ = self._prepare("lesson_quiz", request)
        return await self.delegate.lesson_quiz(request, content, prior_questions)

    async def lesson(self, request, memory, prior_questions=None):
        request, memory = self._prepare("lesson", request, memory)
        return await self.delegate.lesson(request, memory, prior_questions)

    async def review_lesson_alignment(self, request, content, quiz):
        request, _ = self._prepare("review_lesson_alignment", request)
        return await self.delegate.review_lesson_alignment(request, content, quiz)

    async def review_source_claim(self, request):
        request, _ = self._prepare("review_source_claim", request)
        return await self.delegate.review_source_claim(request)

    async def answer(self, request):
        request, _ = self._prepare("answer", request)
        return await self.delegate.answer(request)

    async def answer_stream(self, request):
        request, _ = self._prepare("answer_stream", request)
        async for chunk in self.delegate.answer_stream(request):
            yield chunk

    async def repair_stream(self, request):
        request, _ = self._prepare("repair_stream", request)
        async for chunk in self.delegate.repair_stream(request):
            yield chunk

    async def note(self, request):
        request, _ = self._prepare("note", request)
        return await self.delegate.note(request)

    async def ask_me(self, request):
        request, _ = self._prepare("ask_me", request)
        return await self.delegate.ask_me(request)

    async def replan_book(self, request, memory):
        request, memory = self._prepare("replan_book", request, memory)
        return await self.delegate.replan_book(request, memory)

    async def evaluation_quiz_answers(self, request):
        return await self.delegate.evaluation_quiz_answers(request)

    async def review_evaluation(self, request):
        return await self.delegate.review_evaluation(request)


@dataclass(frozen=True)
class PreparedEpisode:
    database_path: Path
    user_id: str
    series_id: str
    learning_run_id: str
    target_section_id: str
    target_quiz_id: str
    next_section_id: str | None
    prior_sections_completed: int
    source_database_sha256: str


def _ordered_sections(db, series_id: str) -> list[Section]:
    return list(
        db.scalars(
            select(Section)
            .join(Chapter, Chapter.id == Section.chapter_id)
            .join(Book, Book.id == Chapter.book_id)
            .where(Book.series_id == series_id, Book.deleted_at.is_(None))
            .order_by(Book.position, Chapter.position, Section.position)
        ).all()
    )


def _wait_tasks(
    client: TestClient,
    result: dict,
    timeout: float = 30,
    allowed_failure_types: set[str] | None = None,
) -> list[dict]:
    completed: list[dict] = []
    for initial in result.get("workflowTasks", []):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get(f"/api/learning-tasks/{initial['taskId']}")
            if response.status_code != 200:
                raise RuntimeError(
                    f"setup task lookup failed: {response.status_code}"
                )
            payload = response.json()
            if payload.get("status") in {"succeeded", "failed"}:
                if payload["status"] != "succeeded":
                    if payload.get("type") in (allowed_failure_types or set()):
                        completed.append(payload)
                        break
                    raise RuntimeError(
                        f"setup task failed: {payload.get('type')} "
                        f"{payload.get('errorCode')}"
                    )
                completed.append(payload)
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(f"setup task timed out: {initial['taskId']}")
    return completed


def _correct_answers(client: TestClient, quiz_id: str) -> list[list[int]]:
    with client.app.state.sessions() as db:
        quiz = db.get(QuizSet, quiz_id)
        if not quiz:
            raise RuntimeError(f"quiz missing during setup: {quiz_id}")
        return [
            list(item["correct"])
            for item in json.loads(quiz.questions_json)
        ]


def prepare_episode_database(
    *,
    workspace: Path,
    persona: dict[str, Any],
    destination: Path,
) -> PreparedEpisode:
    binding = persona["initialFailureBinding"]
    source = workspace / binding["sourceDatabase"]
    expected_hash = binding["sourceDatabaseSha256"]
    actual_hash = file_sha256(source)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"frozen source hash mismatch for {persona['personaId']}: "
            f"expected {expected_hash}, got {actual_hash}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite episode database: {destination}")
    shutil.copy2(source, destination)
    migrate_episode_database(destination, workspace)

    engine, sessions = build_database(f"sqlite+pysqlite:///{destination}")
    try:
        with sessions() as db:
            target = db.get(Section, binding["sectionId"])
            if not target:
                raise RuntimeError("frozen target section missing")
            chapter = db.get(Chapter, target.chapter_id)
            book = db.get(Book, chapter.book_id)
            shelf = db.get(Shelf, book.shelf_id)
            user_id = shelf.user_id
            active_runs = db.scalars(
                select(LearningRun).where(
                    LearningRun.user_id == user_id,
                    LearningRun.series_id == book.series_id,
                    LearningRun.status == "active",
                )
            ).all()
            mission_id = active_runs[-1].initial_mission_version_id if active_runs else None
            for run in active_runs:
                run.status = "completed"
            learning_run = LearningRun(
                id=f"learning_run_synthetic_{uuid4().hex}",
                user_id=user_id,
                series_id=book.series_id,
                initial_mission_version_id=mission_id,
                status="active",
            )
            db.add(learning_run)
            profile = db.get(UserProfile, user_id)
            if profile:
                profile.stage = persona["profile"]["stage"]
                profile.experience = (
                    persona["profile"]["experience"]
                    + "；合成实验自述："
                    + persona["profile"]["selfReport"]
                )
                profile.version = (profile.version or 0) + 1
            db.flush()
            rebuild_user_projections(db, user_id=user_id)
            db.commit()
            series_id = book.series_id
            run_id = learning_run.id
    finally:
        engine.dispose()

    setup_app = create_app(
        f"sqlite+pysqlite:///{destination}",
        ai=LocalDemoAdapter(),
    )
    prior_count = 0
    with TestClient(setup_app, raise_server_exceptions=False) as client:
        with client.app.state.sessions() as db:
            ordered = _ordered_sections(db, series_id)
            target_index = next(
                index
                for index, section in enumerate(ordered)
                if section.id == binding["sectionId"]
            )
            prior_ids = [section.id for section in ordered[:target_index]]
            next_section_id = (
                ordered[target_index + 1].id
                if target_index + 1 < len(ordered)
                else None
            )
        for section_id in prior_ids:
            state_response = client.get(f"/api/sections/{section_id}")
            if state_response.status_code != 200:
                raise RuntimeError(
                    f"setup could not read {section_id}: "
                    f"{state_response.status_code} {state_response.text[:400]}"
                )
            state = state_response.json()
            if state.get("status") == "completed":
                continue
            if state.get("status") not in {"available", "in_progress"}:
                raise RuntimeError(
                    f"setup section is not available: {section_id} "
                    f"{state.get('status')}"
                )
            quiz_id = state["quiz"]["id"]
            response = client.post(
                f"/api/sections/{section_id}/quiz",
                json={
                    "quizSetId": quiz_id,
                    "answers": _correct_answers(client, quiz_id),
                },
                headers={
                    "Idempotency-Key": (
                        f"synthetic-setup-{FIXTURE_SETUP_VERSION}-{section_id}"
                    )
                },
            )
            if response.status_code != 200 or not response.json().get("passed"):
                raise RuntimeError(
                    f"deterministic setup failed at {section_id}: "
                    f"{response.status_code} {response.text[:600]}"
                )
            _wait_tasks(client, response.json())
            prior_count += 1

        target_response = client.get(f"/api/sections/{binding['sectionId']}")
        if target_response.status_code != 200:
            raise RuntimeError("prepared target section is unreadable")
        target_state = target_response.json()
        if target_state.get("status") not in {"available", "in_progress"}:
            raise RuntimeError(
                f"prepared target is not available: {target_state.get('status')}"
            )
        if target_state.get("quiz", {}).get("id") != binding["quizSetId"]:
            raise RuntimeError(
                "prepared target quiz does not match frozen persona binding"
            )

    return PreparedEpisode(
        database_path=destination,
        user_id=user_id,
        series_id=series_id,
        learning_run_id=run_id,
        target_section_id=binding["sectionId"],
        target_quiz_id=binding["quizSetId"],
        next_section_id=next_section_id,
        prior_sections_completed=prior_count,
        source_database_sha256=actual_hash,
    )


def forced_failure_answers(
    questions: list[dict[str, Any]],
    *,
    primary_question_index: int,
    primary_distractor_indexes: list[int],
) -> tuple[list[list[int]], list[int]]:
    """Return a frozen below-threshold response while preserving other answers."""
    if not questions:
        raise ValueError("cannot force failure for an empty quiz")
    required_wrong = max(1, len(questions) - int(0.79 * len(questions)))
    wrong_indexes = [primary_question_index]
    wrong_indexes.extend(
        index
        for index in range(len(questions))
        if index != primary_question_index
    )
    wrong_indexes = wrong_indexes[:required_wrong]
    answers: list[list[int]] = []
    for index, question in enumerate(questions):
        correct = list(question["correct"])
        if index not in wrong_indexes:
            answers.append(correct)
            continue
        if index == primary_question_index and primary_distractor_indexes:
            distractor = list(primary_distractor_indexes)
        else:
            distractor = next(
                ([option] for option in range(len(question["options"])) if option not in correct),
                [],
            )
        if set(distractor) == set(correct):
            raise ValueError(f"configured distractor is correct for question {index}")
        answers.append(distractor)
    return answers, wrong_indexes


def _question_fingerprint(question: dict[str, Any]) -> str:
    return stable_hash(
        {
            "prompt": question.get("prompt", ""),
            "options": question.get("options", []),
        }
    )


def _quiz_questions(client: TestClient, quiz_id: str) -> list[dict[str, Any]]:
    with client.app.state.sessions() as db:
        quiz = db.get(QuizSet, quiz_id)
        if not quiz:
            raise RuntimeError(f"quiz not found: {quiz_id}")
        return json.loads(quiz.questions_json)


def _progress_status(
    client: TestClient,
    *,
    learning_run_id: str,
    section_id: str | None,
) -> str | None:
    if not section_id:
        return None
    with client.app.state.sessions() as db:
        row = db.scalar(
            select(SectionProgress).where(
                SectionProgress.learning_run_id == learning_run_id,
                SectionProgress.section_id == section_id,
            )
        )
        return row.status if row else None


def _episode_counts(client: TestClient, prepared: PreparedEpisode) -> dict[str, int]:
    with client.app.state.sessions() as db:
        attempt_ids = list(
            db.scalars(
                select(QuizAttempt.id).where(
                    QuizAttempt.learning_run_id == prepared.learning_run_id,
                    QuizAttempt.user_id == prepared.user_id,
                )
            ).all()
        )
        return {
            "attempts": len(attempt_ids),
            "scoringResults": (
                db.scalar(
                    select(func.count()).select_from(ScoringResult).where(
                        ScoringResult.attempt_id.in_(attempt_ids)
                    )
                )
                if attempt_ids
                else 0
            )
            or 0,
            "observations": (
                db.scalar(
                    select(func.count())
                    .select_from(AssessmentObservation)
                    .where(
                        AssessmentObservation.learning_run_id
                        == prepared.learning_run_id
                    )
                )
                or 0
            ),
            "remediations": (
                db.scalar(
                    select(func.count()).select_from(Remediation).where(
                        Remediation.section_id == prepared.target_section_id
                    )
                )
                or 0
            ),
        }


def run_core_episode(
    *,
    prepared: PreparedEpisode,
    persona: dict[str, Any],
    condition: StudyCondition,
    app_delegate,
    learner_answerer,
    execution_mode: Literal["fixture", "real_provider"],
    task_timeout: float = 900,
    source_verifier=None,
) -> dict[str, Any]:
    """Run one public-HTTP synthetic learner journey over a fresh DB clone."""
    started = time.monotonic()
    binding = persona["initialFailureBinding"]
    wrapper = SyntheticAblationAdapter(app_delegate, condition)
    app = create_app(
        f"sqlite+pysqlite:///{prepared.database_path}",
        ai=wrapper,
        source_verifier=source_verifier,
    )
    steps: list[dict[str, Any]] = []
    hard_gates: dict[str, bool] = {}
    error: str | None = None
    final_state: dict[str, Any] = {}
    replacement_quiz_id = ""
    failed_attempt_id = ""
    passed_attempt_id = ""
    retry_attempts = 0
    remediation_blocks: list[dict[str, Any]] = []

    def record(name: str, payload: dict[str, Any]) -> None:
        steps.append({"name": name, **payload})

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            initial_response = client.get(
                f"/api/sections/{prepared.target_section_id}"
            )
            if initial_response.status_code != 200:
                raise RuntimeError(
                    f"target read failed: {initial_response.status_code} "
                    f"{initial_response.text[:500]}"
                )
            initial = initial_response.json()
            if initial.get("quiz", {}).get("id") != prepared.target_quiz_id:
                raise RuntimeError("episode did not start from the frozen quiz")
            original_questions = _quiz_questions(client, prepared.target_quiz_id)
            primary = original_questions[binding["primaryQuestionIndex"]]
            hard_gates["frozen_primary_binding"] = bool(
                primary.get("id") == binding["questionId"]
                and primary.get("assessmentTargetId")
                == binding["expectedAssessmentTargetId"]
            )
            answers, forced_wrong_indexes = forced_failure_answers(
                original_questions,
                primary_question_index=binding["primaryQuestionIndex"],
                primary_distractor_indexes=binding["distractorIndexes"],
            )
            next_before = _progress_status(
                client,
                learning_run_id=prepared.learning_run_id,
                section_id=prepared.next_section_id,
            )
            fail_key = (
                f"synthetic-fail-{persona['personaId']}-{condition}-"
                f"{prepared.learning_run_id}"
            )
            fail_started = time.monotonic()
            failed_response = client.post(
                f"/api/sections/{prepared.target_section_id}/quiz",
                json={
                    "quizSetId": prepared.target_quiz_id,
                    "answers": answers,
                },
                headers={"Idempotency-Key": fail_key},
            )
            grading_seconds = time.monotonic() - fail_started
            if failed_response.status_code != 200:
                raise RuntimeError(
                    f"forced failure submit failed: {failed_response.status_code} "
                    f"{failed_response.text[:600]}"
                )
            failed = failed_response.json()
            failed_attempt_id = failed.get("attemptId", "")
            next_after_failure = _progress_status(
                client,
                learning_run_id=prepared.learning_run_id,
                section_id=prepared.next_section_id,
            )
            hard_gates["forced_failure_recorded"] = bool(
                failed_attempt_id and failed.get("passed") is False
            )
            hard_gates["grading_returned_before_ai"] = grading_seconds < 2
            hard_gates["no_unauthorized_unlock"] = (
                next_after_failure == next_before
                and next_after_failure not in {"available", "completed"}
            )
            with client.app.state.sessions() as db:
                post_failure_state = db.scalar(
                    select(KnowledgeStateProjection).where(
                        KnowledgeStateProjection.user_id == prepared.user_id,
                        KnowledgeStateProjection.assessment_target_id
                        == binding["expectedAssessmentTargetId"],
                    )
                )
                post_failure_claim = (
                    post_failure_state.claim_status
                    if post_failure_state
                    else "unobserved"
                )
            hard_gates["no_mastery_claim_from_failed_attempt"] = (
                post_failure_claim
                not in {"verified_immediate", "verified_delayed", "retained"}
            )
            record(
                "forced_failure",
                {
                    "forcedFailure": True,
                    "attemptId": failed_attempt_id,
                    "quizSetId": prepared.target_quiz_id,
                    "wrongQuestionIndexes": forced_wrong_indexes,
                    "selectedDistractors": [answers[index] for index in forced_wrong_indexes],
                    "gradingSeconds": round(grading_seconds, 4),
                    "nextStatusBefore": next_before,
                    "nextStatusAfter": next_after_failure,
                    "postFailureClaimStatus": post_failure_claim,
                },
            )
            failure_tasks = _wait_tasks(client, failed, timeout=task_timeout)
            record(
                "failure_workflow_tasks",
                {
                    "tasks": [
                        {
                            "taskId": item.get("taskId"),
                            "type": item.get("type"),
                            "status": item.get("status"),
                            "attemptCount": item.get("attemptCount"),
                            "result": item.get("result"),
                        }
                        for item in failure_tasks
                    ]
                },
            )
            remediated_response = client.get(
                f"/api/sections/{prepared.target_section_id}"
            )
            if remediated_response.status_code != 200:
                raise RuntimeError("remediated section could not be read")
            remediated = remediated_response.json()
            replacement_quiz_id = remediated.get("quiz", {}).get("id", "")
            remediation_blocks = list(
                (remediated.get("remediations") or [{}])[-1].get("blocks") or []
            )
            hard_gates["remediation_persisted"] = bool(remediation_blocks)
            hard_gates["replacement_quiz_generated"] = bool(
                replacement_quiz_id
                and replacement_quiz_id != prepared.target_quiz_id
            )

            anchor_blocks = remediated.get("content", {}).get("blocks") or []
            if anchor_blocks:
                ask_response = client.post(
                    f"/api/sections/{prepared.target_section_id}/ask",
                    json={
                        "blockId": anchor_blocks[0]["id"],
                        "question": persona["askAiIntent"],
                        "forceRelation": "new_question",
                    },
                )
                hard_gates["ask_ai_bound_to_section"] = ask_response.status_code == 200
                record(
                    "ask_ai",
                    {
                        "statusCode": ask_response.status_code,
                        "blockId": anchor_blocks[0]["id"],
                        "question": persona["askAiIntent"],
                    },
                )
            else:
                hard_gates["ask_ai_bound_to_section"] = False

            passed_result: dict[str, Any] | None = None
            while retry_attempts < 2:
                retry_attempts += 1
                state_response = client.get(
                    f"/api/sections/{prepared.target_section_id}"
                )
                state = state_response.json()
                quiz = state["quiz"]
                learner_section = {
                    "id": state["id"],
                    "title": state["title"],
                    "question": state["question"],
                    "content": {
                        **state["content"],
                        "blocks": [
                            *state["content"].get("blocks", []),
                            *(
                                (state.get("remediations") or [{}])[-1].get(
                                    "blocks", []
                                )
                            ),
                        ],
                    },
                }
                learner_answers = learner_answerer(learner_section, quiz)
                if len(learner_answers) != len(quiz["questions"]):
                    raise RuntimeError("learner answer count does not match quiz")
                retry_key = (
                    f"synthetic-retry-{persona['personaId']}-{condition}-"
                    f"{prepared.learning_run_id}-{retry_attempts}"
                )
                retry_response = client.post(
                    f"/api/sections/{prepared.target_section_id}/quiz",
                    json={
                        "quizSetId": quiz["id"],
                        "answers": learner_answers,
                    },
                    headers={"Idempotency-Key": retry_key},
                )
                if retry_response.status_code != 200:
                    raise RuntimeError(
                        f"learner retry failed: {retry_response.status_code} "
                        f"{retry_response.text[:600]}"
                    )
                passed_result = retry_response.json()
                passed_attempt_id = passed_result.get("attemptId", "")
                retry_tasks = _wait_tasks(
                    client,
                    passed_result,
                    timeout=task_timeout,
                    allowed_failure_types=(
                        {"next_section_preload"}
                        if execution_mode == "fixture"
                        else None
                    ),
                )
                record(
                    "learner_retry",
                    {
                        "attemptNumber": retry_attempts,
                        "attemptId": passed_attempt_id,
                        "quizSetId": quiz["id"],
                        "passed": passed_result.get("passed"),
                        "workflowTasks": [
                            {
                                "taskId": item.get("taskId"),
                                "type": item.get("type"),
                                "status": item.get("status"),
                                "attemptCount": item.get("attemptCount"),
                                "result": item.get("result"),
                            }
                            for item in retry_tasks
                        ],
                    },
                )
                if passed_result.get("passed"):
                    break

            hard_gates["learner_passed_within_two_attempts"] = bool(
                passed_result and passed_result.get("passed")
            )
            final_state = client.get(
                f"/api/sections/{prepared.target_section_id}"
            ).json()
            next_after_pass = _progress_status(
                client,
                learning_run_id=prepared.learning_run_id,
                section_id=prepared.next_section_id,
            )
            hard_gates["unlock_only_after_pass"] = bool(
                final_state.get("status") == "completed"
                and (
                    prepared.next_section_id is None
                    or next_after_pass in {"preparing", "available", "completed"}
                )
            )

            replacement_questions = _quiz_questions(client, replacement_quiz_id)
            failed_targets = {
                original_questions[index]["assessmentTargetId"]
                for index in forced_wrong_indexes
            }
            replacement_targets = {
                item["assessmentTargetId"] for item in replacement_questions
            }
            original_fingerprints = {
                _question_fingerprint(item) for item in original_questions
            }
            replacement_fingerprints = {
                _question_fingerprint(item) for item in replacement_questions
            }
            with client.app.state.sessions() as db:
                original_quiz = db.get(QuizSet, prepared.target_quiz_id)
                replacement_quiz = db.get(QuizSet, replacement_quiz_id)
                target_state = db.scalar(
                    select(KnowledgeStateProjection).where(
                        KnowledgeStateProjection.user_id == prepared.user_id,
                        KnowledgeStateProjection.assessment_target_id
                        == next(iter(failed_targets), ""),
                    )
                )
                before_rebuild = {
                    "targetStatus": final_state.get("status"),
                    "nextStatus": next_after_pass,
                    "claimStatus": target_state.claim_status if target_state else None,
                }
                hard_gates["replacement_target_complete"] = (
                    failed_targets <= replacement_targets
                )
                hard_gates["replacement_question_novel"] = not bool(
                    original_fingerprints & replacement_fingerprints
                )
                hard_gates["learning_contract_unchanged"] = bool(
                    original_quiz
                    and replacement_quiz
                    and original_quiz.learning_contract_version_id
                    == replacement_quiz.learning_contract_version_id
                )
                rebuild_result = rebuild_user_projections(
                    db,
                    user_id=prepared.user_id,
                )
                db.commit()
                rebuilt_progress = db.scalar(
                    select(SectionProgress).where(
                        SectionProgress.learning_run_id
                        == prepared.learning_run_id,
                        SectionProgress.section_id
                        == prepared.target_section_id,
                    )
                )
                rebuilt_next = (
                    db.scalar(
                        select(SectionProgress).where(
                            SectionProgress.learning_run_id
                            == prepared.learning_run_id,
                            SectionProgress.section_id
                            == prepared.next_section_id,
                        )
                    )
                    if prepared.next_section_id
                    else None
                )
                after_rebuild = {
                    "targetStatus": rebuilt_progress.status if rebuilt_progress else None,
                    "nextStatus": rebuilt_next.status if rebuilt_next else None,
                }
                hard_gates["projection_rebuild_consistent"] = bool(
                    rebuilt_progress
                    and rebuilt_progress.status == "completed"
                    and (
                        not prepared.next_section_id
                        or rebuilt_next
                        and rebuilt_next.status in {"available", "completed"}
                    )
                )

            applicable_audits = [
                item for item in wrapper.context_audit if item["preMemoryCount"] > 0
            ]
            if condition == "NO_MEMORY":
                hard_gates["memory_ablation_exact"] = bool(
                    applicable_audits
                    and all(
                        item["deliveredMemoryCount"] == 0
                        and item["removedEvidenceIds"]
                        for item in applicable_audits
                    )
                )
            else:
                hard_gates["memory_delivery_preserved"] = bool(
                    not persona.get("memoryContrastApplicable")
                    or applicable_audits
                    and all(
                        item["deliveredMemoryCount"] > 0
                        for item in applicable_audits
                    )
                )
            with client.app.state.sessions() as db:
                failed_observations = db.scalars(
                    select(AssessmentObservation).where(
                        AssessmentObservation.attempt_id == failed_attempt_id
                    )
                ).all()
                hard_gates["failed_attempt_observations_persisted"] = bool(
                    failed_observations
                    and any(not item.correct for item in failed_observations)
                )

            counts = _episode_counts(client, prepared)
            record(
                "projection_rebuild",
                {
                    "before": before_rebuild,
                    "after": after_rebuild,
                    "result": rebuild_result,
                },
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:1600]}"
        counts = {}

    passed = not error and all(hard_gates.values())
    return {
        "schemaVersion": "slow_synthetic_episode_v1",
        "executionMode": execution_mode,
        "condition": condition,
        "personaId": persona["personaId"],
        "forcedFailure": True,
        "doNotInterpretAsHumanOutcome": True,
        "fixture": {
            "setupVersion": FIXTURE_SETUP_VERSION,
            "sourceDatabaseSha256": prepared.source_database_sha256,
            "episodeDatabaseSha256": file_sha256(prepared.database_path),
            "priorSectionsCompleted": prepared.prior_sections_completed,
        },
        "lineage": {
            "learningRunId": prepared.learning_run_id,
            "sectionId": prepared.target_section_id,
            "originalQuizSetId": prepared.target_quiz_id,
            "failedAttemptId": failed_attempt_id,
            "replacementQuizSetId": replacement_quiz_id,
            "passedAttemptId": passed_attempt_id,
        },
        "durationSeconds": round(time.monotonic() - started, 3),
        "retryAttempts": retry_attempts,
        "hardGates": hard_gates,
        "contextAudit": wrapper.context_audit,
        "remediation": {
            "blockCount": len(remediation_blocks),
            "blocks": remediation_blocks,
        },
        "steps": steps,
        "databaseCounts": counts,
        "usage": {
            "inputTokens": wrapper.input_tokens,
            "outputTokens": wrapper.output_tokens,
        },
        "error": error,
        "verdict": "PASS" if passed else "FAIL",
    }
