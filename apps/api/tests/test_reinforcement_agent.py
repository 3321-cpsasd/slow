import json

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.ai.contracts import ChoiceQuestion, DistractorDiagnostic, GeneratedQuiz, LessonAlignmentReview
from app.infrastructure.tables import (
    AssessmentAnswerVersion,
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    AssessmentObservation,
    EvidenceQualificationEvent,
    GovernanceDecisionSnapshot,
    ReinforcementEventRecord,
    ReinforcementPackageVersion,
    ReinforcementRun,
    ReviewAssignment,
)
from app.main import create_app
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier

from test_review_assignment_flow import (
    ReviewCapableAi,
    _complete_initial_quiz_and_make_due,
)


class ReinforcementCapableAi(ReviewCapableAi):
    async def lesson_quiz(self, request, content, prior_questions=None):
        if request.get("reviewMode") != "reinforcement_verification":
            generated = await super().lesson_quiz(request, content, prior_questions)
            generated.questions[0].distractor_diagnostics = [
                DistractorDiagnostic(
                    option_index=0,
                    cause_code="prerequisite_gap",
                    rationale="该选项跳过了判断成立所需的前提。",
                ),
                DistractorDiagnostic(
                    option_index=2,
                    cause_code="mechanism_reasoning_break",
                    rationale="该选项只复述题面，没有重建判断机制。",
                ),
            ]
            return generated
        prior = prior_questions[0]
        return GeneratedQuiz(questions=[ChoiceQuestion(
            prompt=f"独立验证：在全新情境中，如何判断 {prior['objective']}？",
            options=["只复述刚才的选项", "检查机制、条件与边界后再判断", "忽略条件直接套结论"],
            correct=[1],
            core=prior.get("core", False),
            objective=prior["objective"],
            explanation="新的情境仍需要依据机制与边界独立判断。",
            answer_authority="demo_fixture_v1",
        )])

    async def review_lesson_alignment(self, request, content, quiz):
        return LessonAlignmentReview(
            allowed=True,
            covered_objectives=[quiz.questions[0].objective],
        )


def _client(tmp_path):
    return TestClient(create_app(
        "sqlite+pysqlite:///:memory:",
        ReinforcementCapableAi(),
        AcceptingSourceVerifier(),
        LocalAttachmentStorage(tmp_path / "reinforcement-attachments"),
    ))


def _failed_review(client):
    _complete_initial_quiz_and_make_due(client)
    assignment_id = client.get("/api/reviews/due?daily_budget=1").json()["items"][0]["assignmentId"]
    started = client.post(f"/api/reviews/{assignment_id}/start")
    assert started.status_code == 200, started.json()
    failed = client.post(
        f"/api/reviews/{assignment_id}/submit",
        headers={"Idempotency-Key": "review-failed-for-reinforcement"},
        json={"answers": [[0]]},
    )
    assert failed.status_code == 200, failed.json()
    assert failed.json()["passed"] is False
    assert failed.json()["reinforcement"] == {
        "available": True,
        "reason": "wake_failed",
    }
    return assignment_id


def _respond(client, run_id, activity_key, suffix, **body):
    response = client.post(
        f"/api/reinforcements/{run_id}/respond",
        headers={"Idempotency-Key": f"reinforcement-{suffix}"},
        json={"activityKey": activity_key, **body},
    )
    assert response.status_code == 200, response.json()
    return response.json()


def test_failed_wake_enters_bounded_reinforcement_and_only_verify_is_evidence(tmp_path):
    with _client(tmp_path) as client:
        assignment_id = _failed_review(client)
        started = client.post(f"/api/reviews/{assignment_id}/reinforcement")
        assert started.status_code == 200, started.json()
        run = started.json()
        run_id = run["runId"]
        assert run["state"] == "diagnose"
        assert run["currentActivity"]["type"] == "diagnose"
        assert run["currentActivity"]["payload"]["hypothesis"]["status"] == "tentative"
        assert run["currentActivity"]["payload"]["hypothesis"]["causeCode"] == "prerequisite_gap"
        assert "options" not in run["currentActivity"]["payload"]
        assert "只有最后一道独立验证题" in run["evidenceBoundary"]
        active = client.get("/api/reinforcements/active")
        assert active.status_code == 200
        assert active.json()["runId"] == run_id
        with client.app.state.sessions() as db:
            target_id = db.get(ReinforcementRun, run_id).assessment_target_id
        by_target = client.post(f"/api/knowledge-targets/{target_id}/reinforcement")
        assert by_target.status_code == 200
        assert by_target.json()["runId"] == run_id

        run = _respond(client, run_id, "diagnose", "diagnose-01", acknowledged=True)
        assert run["state"] == "repair"
        assert run["currentActivity"]["payload"]["case"]["source"] == "原教材中的已发布内容"
        assert "casesByCause" not in run["currentActivity"]["payload"]
        run = _respond(
            client, run_id, "repair", "repair-01",
            responseText="先检查机制是否满足必要条件。",
        )
        assert run["state"] == "recompose"
        run = _respond(client, run_id, "recompose", "recompose-01", selectedOptions=[1])
        assert run["state"] == "verify"
        assert "correct" not in run["currentActivity"]["payload"]["question"]
        run = _respond(client, run_id, "verify", "verify-01", selectedOptions=[1])
        assert run["status"] == "completed"
        assert run["outcome"]["kind"] == "recovered"
        assert run["progress"]["activityCount"] == 4
        assert client.get("/api/reinforcements/active").json() is None

        with client.app.state.sessions() as db:
            stored = db.get(ReinforcementRun, run_id)
            package = db.scalar(select(ReinforcementPackageVersion).where(
                ReinforcementPackageVersion.run_id == run_id,
            ))
            events = db.scalars(select(ReinforcementEventRecord).where(
                ReinforcementEventRecord.run_id == run_id,
            )).all()
            observations = db.scalars(select(AssessmentObservation).where(
                AssessmentObservation.learning_episode_id == f"reinforcement:{run_id}:verify",
            )).all()
            assert stored.status == "completed"
            assert package.status == "published"
            verification_items = db.scalars(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == package.verification_quiz_set_id,
            )).all()
            assert len(verification_items) == 1
            verification_evidence = db.scalars(select(AssessmentItemEvidenceBlock).where(
                AssessmentItemEvidenceBlock.assessment_item_version_id == verification_items[0].id,
            )).all()
            assert verification_evidence
            source_item_id = json.loads(verification_items[0].payload_json)[
                "sourceAssessmentItemVersionId"
            ]
            source_evidence = set(db.scalars(
                select(AssessmentItemEvidenceBlock.content_block_version_id).where(
                    AssessmentItemEvidenceBlock.assessment_item_version_id
                    == source_item_id
                )
            ))
            assert {
                item.content_block_version_id for item in verification_evidence
            } == source_evidence
            assert db.scalar(select(AssessmentAnswerVersion).where(
                AssessmentAnswerVersion.assessment_item_version_id
                == verification_items[0].id
            )) is not None
            governance = db.scalar(select(GovernanceDecisionSnapshot).where(
                GovernanceDecisionSnapshot.quiz_set_id
                == package.verification_quiz_set_id
            ))
            assert governance.mode == "contract_boundary"
            assert len(events) == 4
            assert len(observations) == 1
            assert observations[0].assistance_mode == "unassisted_reinforcement"
            statuses = {
                event.projection_family: event.status
                for event in db.scalars(select(EvidenceQualificationEvent).where(
                    EvidenceQualificationEvent.observation_id == observations[0].id,
                )).all()
            }
            assert statuses == {
                "gate": "ineligible",
                "mastery": "eligible_grouped",
                "retention": "ineligible",
                "rank": "eligible_grouped",
            }


def test_reinforcement_rejects_review_without_immutable_answer_authority(tmp_path):
    with _client(tmp_path) as client:
        assignment_id = _failed_review(client)
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            item_ids = list(db.scalars(select(AssessmentItemVersion.id).where(
                AssessmentItemVersion.quiz_set_id == assignment.review_quiz_set_id,
            )))
            db.execute(delete(AssessmentAnswerVersion).where(
                AssessmentAnswerVersion.assessment_item_version_id.in_(item_ids),
            ))
            db.commit()

        started = client.post(f"/api/reviews/{assignment_id}/reinforcement")
        assert started.status_code == 409, started.json()
        assert started.json()["code"] == "ASSESSMENT_ANSWER_VERSION_MISSING"
        with client.app.state.sessions() as db:
            assert db.scalar(select(ReinforcementRun).where(
                ReinforcementRun.source_review_assignment_id == assignment_id,
            )) is None


def test_failed_recomposition_uses_second_repair_and_failed_verify_stops(tmp_path):
    with _client(tmp_path) as client:
        assignment_id = _failed_review(client)
        run = client.post(f"/api/reviews/{assignment_id}/reinforcement").json()
        run_id = run["runId"]
        _respond(client, run_id, "diagnose", "diagnose-02", acknowledged=True)
        _respond(client, run_id, "repair", "repair-02", responseText="换情境时也要重新检查条件。")
        run = _respond(client, run_id, "recompose", "recompose-02", selectedOptions=[0])
        assert run["state"] == "repair"
        assert run["progress"]["repairRounds"] == 2
        _respond(client, run_id, "repair", "repair-03", responseText="不能跳过边界条件。")
        run = _respond(client, run_id, "verify", "verify-02", selectedOptions=[0])
        assert run["status"] == "replan_required"
        assert run["progress"]["activityCount"] == 5
        assert run["outcome"]["kind"] == "needsReplan"


def test_reinforcement_idempotency_replays_and_rejects_conflict(tmp_path):
    with _client(tmp_path) as client:
        assignment_id = _failed_review(client)
        run_id = client.post(f"/api/reviews/{assignment_id}/reinforcement").json()["runId"]
        first = client.post(
            f"/api/reinforcements/{run_id}/respond",
            headers={"Idempotency-Key": "reinforcement-idempotent"},
            json={"activityKey": "diagnose", "acknowledged": True},
        )
        assert first.status_code == 200
        replay = client.post(
            f"/api/reinforcements/{run_id}/respond",
            headers={"Idempotency-Key": "reinforcement-idempotent"},
            json={"activityKey": "diagnose", "acknowledged": True},
        )
        assert replay.status_code == 200
        conflict = client.post(
            f"/api/reinforcements/{run_id}/respond",
            headers={"Idempotency-Key": "reinforcement-idempotent"},
            json={"activityKey": "diagnose", "acknowledged": False},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
