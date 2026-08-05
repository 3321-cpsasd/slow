from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.ai.contracts import ChoiceQuestion, GeneratedQuiz
from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentObservation,
    EvidenceQualificationEvent,
    QuizSet,
    ReviewAssignment,
    ReviewAssignmentEventRecord,
    ReviewState,
    now,
)
from app.main import create_app
from app.modules.learning.reviews import ReviewAssignmentService
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier

from test_vertical_slice import FakeAi, create_series, wait_for_task


class ReviewCapableAi(FakeAi):
    async def lesson_quiz(self, request, content, prior_questions=None):
        assert request["reviewMode"] == "delayed_assignment"
        prior = prior_questions[0]
        return GeneratedQuiz(questions=[ChoiceQuestion(
            prompt=f"延迟复习：{prior['objective']} 的反例边界是什么？",
            options=["忽略前提", "检查结论在边界条件下是否仍成立", "复述原题"],
            correct=[1],
            core=prior.get("core", False),
            objective=prior["objective"],
            explanation="延迟复习需要把机制迁移到新的边界判断。",
        )])


class ReusingReviewAi(ReviewCapableAi):
    async def lesson_quiz(self, request, content, prior_questions=None):
        prior = prior_questions[0]
        return GeneratedQuiz(questions=[ChoiceQuestion(
            prompt=prior["prompt"],
            options=prior["options"],
            correct=prior["correct"],
            core=prior.get("core", False),
            objective=prior["objective"],
            explanation=prior["explanation"],
        )])


def _review_client(tmp_path, ai=None):
    return TestClient(create_app(
        "sqlite+pysqlite:///:memory:",
        ai or ReviewCapableAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "review-attachments"),
    ))


def _complete_initial_quiz_and_make_due(client):
    series = create_series(client)
    assert wait_for_task(
        client,
        series["initializationTask"]["taskId"],
    )["status"] == "succeeded"
    view = client.get(f"/api/series/{series['id']}").json()
    section_id = view["books"][0]["chapters"][0]["sections"][0]["id"]
    section = client.get(f"/api/sections/{section_id}").json()
    response = client.post(
        f"/api/sections/{section_id}/quiz",
        json={
            "quizSetId": section["quiz"]["id"],
            "answers": [[1], [1], [1], [1], [1]],
        },
    )
    assert response.status_code == 200, response.json()
    for task in response.json().get("workflowTasks", []):
        assert wait_for_task(client, task["taskId"])["status"] == "succeeded"
    with client.app.state.sessions() as db:
        review = db.scalar(select(ReviewState).where(ReviewState.user_id == "user_demo"))
        review.status = "scheduled"
        review.next_due_at = now() - timedelta(hours=1)
        db.commit()
    return section_id


def test_review_assignment_materializes_once_and_submits_candidate(tmp_path):
    with _review_client(tmp_path) as client:
        section_id = _complete_initial_quiz_and_make_due(client)

        first = client.get("/api/reviews/due?daily_budget=1")
        assert first.status_code == 200, first.json()
        due = first.json()
        assert due["selectedCount"] == 1
        assert due["items"][0]["status"] == "presented"
        assignment_id = due["items"][0]["assignmentId"]

        repeated = client.get("/api/reviews/due?daily_budget=99").json()
        assert repeated["selectionRunId"] == due["selectionRunId"]
        assert repeated["items"][0]["assignmentId"] == assignment_id
        assert repeated["dailyBudget"] == 1

        started = client.post(f"/api/reviews/{assignment_id}/start")
        assert started.status_code == 200, started.json()
        started_body = started.json()
        assert started_body["status"] == "started"
        assert len(started_body["quiz"]["questions"]) == 1
        assert "correct" not in started_body["quiz"]["questions"][0]
        assert "explanation" not in started_body["quiz"]["questions"][0]
        assert "claim_block_indexes" not in started_body["quiz"]["questions"][0]
        assert started_body["quiz"]["questions"][0]["selectionMode"] == "single"

        submitted = client.post(
            f"/api/reviews/{assignment_id}/submit",
            headers={"Idempotency-Key": "review-submit-0001"},
            json={"answers": [[1]]},
        )
        assert submitted.status_code == 200, submitted.json()
        result = submitted.json()
        assert result["status"] == "submitted"
        assert result["retentionQualification"]["status"] == "candidate"

        section = client.get(f"/api/sections/{section_id}").json()
        assert section["latestAttemptReview"]["total"] == 5
        assert len(section["latestAttemptReview"]["questions"]) == 5
        assert section["latestAttemptReview"]["attemptId"] != result["attemptId"]

        replay = client.post(
            f"/api/reviews/{assignment_id}/submit",
            headers={"Idempotency-Key": "review-submit-0001"},
            json={"answers": [[1]]},
        )
        assert replay.status_code == 200
        assert replay.json() == result
        conflict = client.post(
            f"/api/reviews/{assignment_id}/submit",
            headers={"Idempotency-Key": "review-submit-0001"},
            json={"answers": [[0]]},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            observations = db.scalars(
                select(AssessmentObservation).where(
                    AssessmentObservation.attempt_id == assignment.submitted_attempt_id
                )
            ).all()
            assert len(observations) == 1
            assert observations[0].assistance_mode == "unassisted_review"
            assert observations[0].learning_episode_id == f"review:{assignment_id}"
            statuses = {
                item.projection_family: item.status
                for item in db.scalars(
                    select(EvidenceQualificationEvent).where(
                        EvidenceQualificationEvent.observation_id == observations[0].id
                    )
                ).all()
            }
            assert statuses == {
                "gate": "ineligible",
                "mastery": "eligible_grouped",
                "retention": "candidate",
            }
            assert db.scalar(
                select(func.count(ReviewAssignmentEventRecord.id)).where(
                    ReviewAssignmentEventRecord.assignment_id == assignment_id
                )
            ) == 3


def test_review_selection_does_not_duplicate_an_unfinished_assignment_next_day(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        first = client.get("/api/reviews/due?daily_budget=1").json()
        assignment_id = first["items"][0]["assignmentId"]

        with client.app.state.sessions() as db:
            next_day = ReviewAssignmentService(
                db,
                user_id="user_demo",
                ai=client.app.state.ai,
            ).due(
                daily_budget=1,
                as_of=now() + timedelta(days=1),
            )
            assignments = db.scalars(select(ReviewAssignment)).all()

        assert next_day["selectedCount"] == 0
        assert len(assignments) == 1
        assert assignments[0].id == assignment_id
        assert assignments[0].status == "presented"


def test_review_skip_is_terminal_and_creates_no_observation(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        due = client.get("/api/reviews/due?daily_budget=1").json()
        assignment_id = due["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            before = db.scalar(select(func.count(AssessmentObservation.id)))

        skipped = client.post(f"/api/reviews/{assignment_id}/skip")
        assert skipped.status_code == 200
        assert skipped.json()["status"] == "skipped"
        assert client.post(f"/api/reviews/{assignment_id}/skip").status_code == 200
        blocked = client.post(f"/api/reviews/{assignment_id}/start")
        assert blocked.status_code == 409

        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count(AssessmentObservation.id))) == before


def test_review_start_rejects_reused_question_without_state_change(tmp_path):
    with _review_client(tmp_path, ReusingReviewAi()) as client:
        _complete_initial_quiz_and_make_due(client)
        due = client.get("/api/reviews/due?daily_budget=1").json()
        assignment_id = due["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            before_quizzes = db.scalar(select(func.count(QuizSet.id)))

        response = client.post(f"/api/reviews/{assignment_id}/start")
        assert response.status_code == 502
        assert response.json()["code"] == "REVIEW_QUIZ_NOT_NOVEL"

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            assert assignment.status == "presented"
            assert assignment.review_quiz_set_id is None
            assert db.scalar(select(func.count(QuizSet.id))) == before_quizzes


def test_review_assignment_is_hidden_from_other_user(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        assignment_id = client.get(
            "/api/reviews/due?daily_budget=1"
        ).json()["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            service = ReviewAssignmentService(
                db,
                user_id="user_other",
                ai=client.app.state.ai,
            )
            with pytest.raises(AppError) as error:
                service.skip(assignment_id)
            assert error.value.status == 404
            assert error.value.code == "REVIEW_ASSIGNMENT_NOT_FOUND"
