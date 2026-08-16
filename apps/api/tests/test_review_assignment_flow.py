from datetime import timedelta
import json
import logging
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select

from app.ai.contracts import (
    CapabilityReviewRubricCriterion,
    CapabilityReviewTaskCandidate,
    ChoiceQuestion,
    GeneratedQuiz,
    LessonAlignmentIssue,
    LessonAlignmentReview,
    StandardApplicationCriterionResult,
    StandardApplicationEvaluation,
)
from app.core.errors import AppError
from app.infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    AssessmentAnswerVersion,
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    CapabilityStageCriterion,
    CapabilityStateProjection,
    ContentBlockAssessmentTarget,
    ContentBlockVersion,
    ContentVersion,
    EvidenceQualificationEvent,
    GovernanceDecisionSnapshot,
    LearningContractAssessmentTarget,
    QuizSet,
    ReviewAssignment,
    ReviewAssignmentEventRecord,
    ReviewState,
    now,
)
from app.main import create_app
from app.modules.learning.assessment import record_ask_me_assessment_facts
from app.modules.learning.reviews import ReviewAssignmentService
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier

from test_vertical_slice import FakeAi, create_series, wait_for_task


class ReviewCapableAi(FakeAi):
    last_deployment_id = ""
    last_model_family_id = ""

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
            answer_authority="demo_fixture_v1",
        )])

    async def review_lesson_alignment(self, request, content, quiz):
        return LessonAlignmentReview(
            allowed=True,
            covered_objectives=[quiz.questions[0].objective],
        )

    async def author_capability_review_task(self, request):
        self.last_deployment_id = "review-author"
        self.last_model_family_id = "review-author-family"
        self.model = "review-author-model"
        rubric_sources = list(request["plannedCriteria"])
        if len(rubric_sources) == 1:
            rubric_sources.append(request["plannedCriteria"][0])
        return CapabilityReviewTaskCandidate(
            prompt=(
                "请用一个正文没有出现过的运行故障，重新解释当前能力的关键机制与失效边界，"
                "并说明两个判断如何共同约束最终处理方案。"
            ),
            task_context="一个延迟复习时生成的全新运行故障",
            deliverables=["机制解释", "边界判断", "综合结论"],
            rubric=[
                CapabilityReviewRubricCriterion(
                    criterion_key=f"C{index}",
                    stage_criterion_id=item["id"],
                    statement=item["statement"],
                )
                for index, item in enumerate(rubric_sources, 1)
            ],
            reference_answer_points=[
                item["statement"] for item in request["plannedCriteria"]
            ],
            novelty_basis="题面使用新的运行故障，没有复述正文示例。",
        )

    async def evaluate_capability_review_submission(self, request):
        self.last_deployment_id = "review-evaluator"
        self.last_model_family_id = "review-evaluator-family"
        self.model = "review-evaluator-model"
        return StandardApplicationEvaluation(
            verdict="pass",
            evidence_sufficiency="sufficient",
            criterion_results=[
                StandardApplicationCriterionResult(
                    criterion_key=item["criterionKey"],
                    satisfied=True,
                    rationale="提交重新展示了该项当前阶段能力。",
                )
                for item in request["rubric"]
            ],
            rationale="当前阶段能力仍可独立调用。",
        )


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
            answer_authority="demo_fixture_v1",
        )])


class RejectingAlignmentReviewAi(ReviewCapableAi):
    async def review_lesson_alignment(self, request, content, quiz):
        if request.get("reviewMode") != "delayed_assignment":
            return await super().review_lesson_alignment(
                request,
                content,
                quiz,
            )
        return LessonAlignmentReview(
            allowed=False,
            issues=[LessonAlignmentIssue(
                code="quiz_not_grounded",
                severity="blocking",
                message="模型标记的正确答案无法由原正文确定。",
                question_indexes=[0],
            )],
            covered_objectives=[],
        )


class LegacyAnswerReviewAi(ReviewCapableAi):
    async def lesson_quiz(self, request, content, prior_questions=None):
        generated = await super().lesson_quiz(request, content, prior_questions)
        generated.questions[0].answer_authority = "legacy_author_declared"
        return generated


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
        item_rows = db.scalars(
            select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == section["quiz"]["id"]
            )
        ).all()
        item_by_id = {item.id: item for item in item_rows}
        evidence_rows = db.scalars(
            select(AssessmentItemEvidenceBlock).where(
                AssessmentItemEvidenceBlock.assessment_item_version_id.in_(
                    list(item_by_id)
                )
            )
        ).all()
        seen_block_targets = set()
        for evidence in evidence_rows:
            item = item_by_id[evidence.assessment_item_version_id]
            identity = (
                evidence.content_block_version_id,
                item.assessment_target_id,
            )
            if identity in seen_block_targets:
                continue
            seen_block_targets.add(identity)
            db.add(ContentBlockAssessmentTarget(
                id=f"test_block_target_{uuid4().hex}",
                content_block_version_id=evidence.content_block_version_id,
                assessment_target_id=item.assessment_target_id,
                binding_role="teaches",
            ))
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
        assert due["items"][0]["reviewReason"] == "这项能力已经到复习时间"
        assert due["items"][0]["capability"]["currentStage"] == "bronze"
        assert due["items"][0]["taskPlan"]["reactivation"]["taskKind"] == "choice_reactivation"
        assert due["items"][0]["taskPlan"]["reactivation"]["evidenceEffect"] == "activation_only"
        assert due["items"][0]["taskPlan"]["strengthening"]["taskKind"] == "oral_strengthening"
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
        assert not {
            "id",
            "itemKey",
            "optionIds",
            "answerAuthority",
            "optionVerdicts",
            "evidenceBlockIds",
            "sourceQuizSetId",
            "sourceAssessmentItemVersionId",
            "publicationRuleVersion",
            "equivalenceGroupId",
            "assessmentTargetId",
        }.intersection(started_body["quiz"]["questions"][0])
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            governance = db.scalar(
                select(GovernanceDecisionSnapshot).where(
                    GovernanceDecisionSnapshot.quiz_set_id
                    == assignment.review_quiz_set_id
                )
            )
            stored_quiz = db.get(QuizSet, assignment.review_quiz_set_id)
            stored_question = json.loads(stored_quiz.questions_json)[0]
            assert governance is not None
            assert governance.allowed is True
            assert governance.assessment_eligible is True
            assert governance.mode == "contract_boundary"
            assert governance.actor_kind == "review_assignment"
            assert governance.actor_id == assignment.id
            assert "claim_block_indexes" not in stored_question
            assert stored_question["evidenceBlockIds"]
            source_item_id = stored_question["sourceAssessmentItemVersionId"]
            derived_item = db.scalar(
                select(AssessmentItemVersion).where(
                    AssessmentItemVersion.quiz_set_id == stored_quiz.id
                )
            )
            assert db.scalar(
                select(AssessmentAnswerVersion).where(
                    AssessmentAnswerVersion.assessment_item_version_id
                    == derived_item.id
                )
            ) is not None
            source_evidence = set(db.scalars(
                select(AssessmentItemEvidenceBlock.content_block_version_id).where(
                    AssessmentItemEvidenceBlock.assessment_item_version_id
                    == source_item_id
                )
            ))
            derived_evidence = set(db.scalars(
                select(AssessmentItemEvidenceBlock.content_block_version_id).where(
                    AssessmentItemEvidenceBlock.assessment_item_version_id
                    == derived_item.id
                )
            ))
            assert derived_evidence == source_evidence

        repeated_start = client.post(f"/api/reviews/{assignment_id}/start")
        assert repeated_start.status_code == 200
        assert repeated_start.json() == started_body
        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count(QuizSet.id)).where(
                QuizSet.id == started_body["quiz"]["id"]
            )) == 1

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
                "rank": "eligible_grouped",
                "capability": "eligible_grouped",
            }
            assert db.scalar(
                select(func.count(ReviewAssignmentEventRecord.id)).where(
                    ReviewAssignmentEventRecord.assignment_id == assignment_id
                )
            ) == 3


def test_silver_review_freezes_oral_reactivation_instead_of_choice_quiz(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        with client.app.state.sessions() as db:
            source = db.scalar(
                select(AssessmentObservation)
                .where(AssessmentObservation.user_id == "user_demo")
                .order_by(AssessmentObservation.sequence.desc())
            )
            target_rows = db.execute(
                select(LearningContractAssessmentTarget, AssessmentTarget)
                .join(
                    AssessmentTarget,
                    AssessmentTarget.id
                    == LearningContractAssessmentTarget.assessment_target_id,
                )
                .where(
                    LearningContractAssessmentTarget.contract_version_id
                    == source.learning_contract_version_id,
                    LearningContractAssessmentTarget.diagnostic_only.is_(True),
                    AssessmentTarget.dimension.in_({"mechanism", "boundary"}),
                )
            ).all()
            for _binding, target in target_rows:
                record_ask_me_assessment_facts(
                    db,
                    learning_run_id=source.learning_run_id,
                    user_id="user_demo",
                    section_id=source.section_id,
                    learning_contract_version_id=source.learning_contract_version_id,
                    content_version_id=source.content_version_id,
                    assessment_target_ids=[target.id],
                    source_type="ask_me_topic",
                    source_id=f"silver_review_{target.dimension}",
                    evaluation="strong",
                    dimension=target.dimension,
                    payload={},
                )
            review = db.scalar(
                select(ReviewState).where(ReviewState.user_id == "user_demo")
            )
            review.status = "scheduled"
            review.next_due_at = now() - timedelta(hours=1)
            db.commit()

        due = client.get("/api/reviews/due?daily_budget=1")
        assert due.status_code == 200, due.json()
        item = due.json()["items"][0]
        assert item["taskPlan"]["reactivation"]["taskKind"] == "oral_reactivation"
        assert len(item["taskPlan"]["reactivation"]["criterionIds"]) == 2
        assert item["taskPlan"]["strengthening"] is None

        started = client.post(f"/api/reviews/{item['assignmentId']}/start")
        assert started.status_code == 200, started.json()
        assert started.json()["status"] == "started"
        assert started.json()["quiz"] is None
        assert started.json()["capabilityTask"]["taskKind"] == "oral_reactivation"

        submitted = client.post(
            f"/api/reviews/{item['assignmentId']}/respond",
            headers={"Idempotency-Key": "silver-review-response-001"},
            json={
                "response": {
                    "mechanism": "机制说明与新的故障案例推理",
                    "boundary": "列出失效条件并解释为什么会失效",
                },
                "assistanceUsed": False,
            },
        )
        assert submitted.status_code == 200, submitted.json()
        assert submitted.json()["reactivationQualified"] is True
        assert submitted.json()["stageChanged"] is False
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, item["assignmentId"])
            state = db.scalar(
                select(CapabilityStateProjection).where(
                    CapabilityStateProjection.user_id == "user_demo"
                )
            )
            observation = db.scalar(
                select(AssessmentObservation).where(
                    AssessmentObservation.source_type == "capability_review"
                )
            )
            qualifications = {
                row.projection_family: row.status
                for row in db.scalars(
                    select(EvidenceQualificationEvent).where(
                    EvidenceQualificationEvent.observation_id == observation.id,
                )
                ).all()
            }
            assert assignment.status == "submitted"
            assert assignment.review_quiz_set_id is None
            assert state.current_stage == "silver"
            assert state.activation_state == "available"
            assert qualifications["mastery"] == "ineligible"
            assert qualifications["capability"] == "ineligible"
            assert qualifications["retention"] == "candidate"


def test_review_start_accepts_published_v2_content_blocks(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        due = client.get("/api/reviews/due?daily_budget=1").json()
        assignment_id = due["items"][0]["assignmentId"]

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            content = db.get(ContentVersion, assignment.content_version_id)
            blocks = json.loads(content.blocks_json)
            v2_roles = [
                ("core_instruction", "core"),
                ("mechanism", "mechanism"),
                ("comparison", "comparison"),
                ("boundary", "boundary"),
                ("practice", "practice"),
            ]
            for index, block in enumerate(blocks):
                role, relation = v2_roles[index % len(v2_roles)]
                block.update({
                    "blockKey": f"review_block_{index + 1}",
                    "role": role,
                    "relationToAnchor": relation,
                    "assessmentTargetIds": [assignment.assessment_target_id],
                    "knowledgeClaimVersionIds": [],
                })
            blocks[0]["kind"] = "ordered_steps"
            blocks[0]["content"] = "1. 识别目标\n2. 检查边界"
            content.blocks_json = json.dumps(blocks, ensure_ascii=False)
            db.commit()

        started = client.post(f"/api/reviews/{assignment_id}/start")
        assert started.status_code == 200, started.json()
        assert started.json()["status"] == "started"


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


def test_review_start_rejects_semantically_invalid_answer_without_state_change(tmp_path):
    with _review_client(tmp_path, RejectingAlignmentReviewAi()) as client:
        _complete_initial_quiz_and_make_due(client)
        due = client.get("/api/reviews/due?daily_budget=1").json()
        assignment_id = due["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            before_quizzes = db.scalar(select(func.count(QuizSet.id)))

        response = client.post(f"/api/reviews/{assignment_id}/start")
        assert response.status_code == 502
        assert response.json()["code"] == "REVIEW_SEMANTIC_ALIGNMENT_FAILED"

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            assert assignment.status == "presented"
            assert assignment.review_quiz_set_id is None
            assert db.scalar(select(func.count(QuizSet.id))) == before_quizzes


def test_review_start_persists_alignment_gated_legacy_answer(tmp_path):
    with _review_client(tmp_path, LegacyAnswerReviewAi()) as client:
        _complete_initial_quiz_and_make_due(client)
        assignment_id = client.get(
            "/api/reviews/due?daily_budget=1"
        ).json()["items"][0]["assignmentId"]

        response = client.post(f"/api/reviews/{assignment_id}/start")
        assert response.status_code == 200, response.json()
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            item = db.scalar(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == assignment.review_quiz_set_id
            ))
            answer = db.scalar(select(AssessmentAnswerVersion).where(
                AssessmentAnswerVersion.assessment_item_version_id == item.id
            ))
            assert assignment.status == "started"
            assert answer.authority_kind == "alignment_gated_model_v1"
            assert answer.rule_version == "answer_after_semantic_alignment_v1"


def test_review_accepts_legacy_source_answer_but_publishes_new_answer_version(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        with client.app.state.sessions() as db:
            review = db.scalar(select(ReviewState).where(
                ReviewState.user_id == "user_demo"
            ))
            observation = db.scalar(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.assessment_target_id
                    == review.assessment_target_id,
                )
                .order_by(AssessmentObservation.sequence.desc())
            )
            source_quiz = db.get(QuizSet, observation.quiz_set_id)
            source_questions = json.loads(source_quiz.questions_json)
            source_items = db.scalars(
                select(AssessmentItemVersion)
                .where(AssessmentItemVersion.quiz_set_id == source_quiz.id)
                .order_by(AssessmentItemVersion.position)
            ).all()
            for item, question in zip(source_items, source_questions, strict=True):
                item.payload_json = json.dumps(question, ensure_ascii=False)
            db.execute(delete(AssessmentAnswerVersion).where(
                AssessmentAnswerVersion.assessment_item_version_id.in_(
                    [item.id for item in source_items]
                )
            ))
            db.commit()

        due = client.get("/api/reviews/due?daily_budget=1")
        assert due.status_code == 200, due.json()
        assignment_id = due.json()["items"][0]["assignmentId"]
        started = client.post(f"/api/reviews/{assignment_id}/start")
        assert started.status_code == 200, started.json()

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            derived_item = db.scalar(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == assignment.review_quiz_set_id
            ))
            answer = db.scalar(select(AssessmentAnswerVersion).where(
                AssessmentAnswerVersion.assessment_item_version_id == derived_item.id
            ))
            assert answer is not None
            assert answer.publication_status == "published"


def test_review_submit_requires_current_eligible_governance_snapshot(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        assignment_id = client.get(
            "/api/reviews/due?daily_budget=1"
        ).json()["items"][0]["assignmentId"]
        started = client.post(f"/api/reviews/{assignment_id}/start")
        assert started.status_code == 200

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            snapshots = db.scalars(
                select(GovernanceDecisionSnapshot).where(
                    GovernanceDecisionSnapshot.quiz_set_id
                    == assignment.review_quiz_set_id
                )
            ).all()
            assert snapshots
            for snapshot in snapshots:
                db.delete(snapshot)
            db.commit()

        response = client.post(
            f"/api/reviews/{assignment_id}/submit",
            headers={"Idempotency-Key": "review-submit-no-governance"},
            json={"answers": [[1]]},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "REVIEW_QUIZ_GOVERNANCE_REQUIRED"

        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            assert assignment.status == "started"
            assert assignment.submitted_attempt_id is None


def test_review_source_uses_the_exact_observed_item_when_targets_repeat(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        due = client.get("/api/reviews/due?daily_budget=1").json()
        assignment_id = due["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            observation = db.scalar(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.quiz_set_id
                    == assignment.prior_quiz_set_id,
                    AssessmentObservation.assessment_target_id
                    == assignment.assessment_target_id,
                )
                .order_by(AssessmentObservation.sequence.desc())
            )
            expected_item = db.scalar(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == assignment.prior_quiz_set_id,
                AssessmentItemVersion.position == observation.question_index,
            ))

        started = client.post(f"/api/reviews/{assignment_id}/start")
        assert started.status_code == 200, started.json()
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            quiz = db.get(QuizSet, assignment.review_quiz_set_id)
            question = json.loads(quiz.questions_json)[0]
            assert question["sourceAssessmentItemVersionId"] == expected_item.id


def test_review_selection_skips_source_without_immutable_evidence(tmp_path, caplog):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        with client.app.state.sessions() as db:
            review = db.scalar(select(ReviewState).where(
                ReviewState.user_id == "user_demo"
            ))
            observation = db.scalar(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.assessment_target_id
                    == review.assessment_target_id,
                )
                .order_by(AssessmentObservation.sequence.desc())
            )
            item = db.scalar(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == observation.quiz_set_id,
                AssessmentItemVersion.position == observation.question_index,
            ))
            db.execute(delete(AssessmentItemEvidenceBlock).where(
                AssessmentItemEvidenceBlock.assessment_item_version_id == item.id
            ))
            db.commit()

        with caplog.at_level("INFO", logger="app.modules.learning.reviews"):
            due = client.get("/api/reviews/due?daily_budget=1")
        assert due.status_code == 200
        assert due.json()["selectedCount"] == 0
        skipped = next(
            record
            for record in caplog.records
            if record.getMessage().startswith(
                "review source selection skipped incompatible observations"
            )
        )
        assert skipped.rejection_codes[
            "ASSESSMENT_ITEM_EVIDENCE_INCOMPLETE"
        ] >= 1
        with client.app.state.sessions() as db:
            assert db.scalar(select(func.count(ReviewAssignment.id))) == 0


def test_review_start_rejects_cross_content_source_evidence_without_partial_quiz(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        assignment_id = client.get(
            "/api/reviews/due?daily_budget=1"
        ).json()["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            observation = db.scalar(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.quiz_set_id
                    == assignment.prior_quiz_set_id,
                    AssessmentObservation.assessment_target_id
                    == assignment.assessment_target_id,
                )
                .order_by(AssessmentObservation.sequence.desc())
            )
            item = db.scalar(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == assignment.prior_quiz_set_id,
                AssessmentItemVersion.position == observation.question_index,
            ))
            binding = db.scalar(select(AssessmentItemEvidenceBlock).where(
                AssessmentItemEvidenceBlock.assessment_item_version_id == item.id
            ))
            foreign_block = db.scalar(select(ContentBlockVersion).where(
                ContentBlockVersion.content_version_id
                != assignment.content_version_id
            ))
            payload = json.loads(item.payload_json)
            payload["evidenceBlockIds"] = [foreign_block.id]
            item.payload_json = json.dumps(payload, ensure_ascii=False)
            binding.content_block_version_id = foreign_block.id
            before_quizzes = db.scalar(select(func.count(QuizSet.id)))
            db.commit()

        response = client.post(f"/api/reviews/{assignment_id}/start")
        assert response.status_code == 409
        assert response.json()["code"] == "DERIVED_QUIZ_SOURCE_EVIDENCE_CROSS_CONTENT"
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            assert assignment.status == "presented"
            assert assignment.review_quiz_set_id is None
            assert db.scalar(select(func.count(QuizSet.id))) == before_quizzes


def test_review_start_rejects_source_evidence_that_no_longer_teaches_target(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        assignment_id = client.get(
            "/api/reviews/due?daily_budget=1"
        ).json()["items"][0]["assignmentId"]
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            observation = db.scalar(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.quiz_set_id
                    == assignment.prior_quiz_set_id,
                    AssessmentObservation.assessment_target_id
                    == assignment.assessment_target_id,
                )
                .order_by(AssessmentObservation.sequence.desc())
            )
            item = db.scalar(select(AssessmentItemVersion).where(
                AssessmentItemVersion.quiz_set_id == assignment.prior_quiz_set_id,
                AssessmentItemVersion.position == observation.question_index,
            ))
            evidence_ids = list(db.scalars(
                select(AssessmentItemEvidenceBlock.content_block_version_id).where(
                    AssessmentItemEvidenceBlock.assessment_item_version_id == item.id
                )
            ))
            db.execute(delete(ContentBlockAssessmentTarget).where(
                ContentBlockAssessmentTarget.content_block_version_id.in_(evidence_ids),
                ContentBlockAssessmentTarget.assessment_target_id
                == assignment.assessment_target_id,
            ))
            before_quizzes = db.scalar(select(func.count(QuizSet.id)))
            db.commit()

        response = client.post(f"/api/reviews/{assignment_id}/start")
        assert response.status_code == 409
        assert response.json()["code"] == "DERIVED_QUIZ_SOURCE_EVIDENCE_TARGET_MISMATCH"
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            assert assignment.status == "presented"
            assert assignment.review_quiz_set_id is None
            assert db.scalar(select(func.count(QuizSet.id))) == before_quizzes


def test_review_selection_skips_source_with_invalid_governance(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        with client.app.state.sessions() as db:
            review = db.scalar(select(ReviewState).where(
                ReviewState.user_id == "user_demo"
            ))
            observation = db.scalar(
                select(AssessmentObservation)
                .where(
                    AssessmentObservation.assessment_target_id
                    == review.assessment_target_id,
                )
                .order_by(AssessmentObservation.sequence.desc())
            )
            snapshots = db.scalars(select(GovernanceDecisionSnapshot).where(
                GovernanceDecisionSnapshot.quiz_set_id == observation.quiz_set_id
            )).all()
            assert snapshots
            for snapshot in snapshots:
                snapshot.allowed = False
                snapshot.assessment_eligible = False
            db.commit()

        due = client.get("/api/reviews/due?daily_budget=1")
        assert due.status_code == 200
        assert due.json()["selectedCount"] == 0


def test_review_submit_ignores_tampered_quiz_compatibility_projection(tmp_path):
    with _review_client(tmp_path) as client:
        _complete_initial_quiz_and_make_due(client)
        assignment_id = client.get(
            "/api/reviews/due?daily_budget=1"
        ).json()["items"][0]["assignmentId"]
        assert client.post(f"/api/reviews/{assignment_id}/start").status_code == 200
        with client.app.state.sessions() as db:
            assignment = db.get(ReviewAssignment, assignment_id)
            quiz = db.get(QuizSet, assignment.review_quiz_set_id)
            projected = json.loads(quiz.questions_json)
            projected[0]["correct"] = [0]
            projected[0]["prompt"] = "被篡改的兼容投影"
            quiz.questions_json = json.dumps(projected, ensure_ascii=False)
            db.commit()

        submitted = client.post(
            f"/api/reviews/{assignment_id}/submit",
            headers={"Idempotency-Key": "review-submit-authority-only"},
            json={"answers": [[1]]},
        )
        assert submitted.status_code == 200, submitted.json()
        assert submitted.json()["passed"] is True


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
