"""Learning-start preferences, chapter choices, and chapter diagnostics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...domain.learning import grade_choice_quiz
from ...infrastructure.tables import (
    Book,
    BookProgress,
    Chapter,
    ChapterChallengeAttempt,
    ChapterProgress,
    ChapterRouteDecisionEvent,
    ConceptRelationVersion,
    ConceptRevision,
    KnowledgeGraphRelease,
    LearningRunSectionBinding,
    LearningStartPreview,
    QuizAttempt,
    QuizSet,
    Section,
    SectionProgress,
    SeriesLearningStartPreference,
    now,
)
from .assessment import (
    record_scoring_facts,
    section_gate_decision,
)
from .assessment_items import immutable_questions_for_quiz
from .commands import SubmitQuiz
from .content_governance_store import governance_view_for_quiz
from .decision_snapshots import (
    append_assessment_gate_snapshot,
    append_capability_settlement_snapshot,
)
from .capability_profiles import capability_state_views_for_targets
from .progress import best_score_pair


LEARNING_START_PREVIEW_SCHEMA_VERSION = "learning_start_preview_v1"
LEARNING_START_SELECTION_RULE_VERSION = "learning_start_selection_v1"
LEARNING_GOAL_INTERVIEW_RULE_VERSION = "learning_goal_interview_v3"
LEARNING_GOAL_INTERVIEW_MAX_ANSWERS = 8
CHAPTER_ROUTE_RULE_VERSION = "chapter_route_choice_v1"
CHAPTER_CHALLENGE_RULE_VERSION = "chapter_challenge_v1"

RELATION_LABELS = {
    "prerequisite_for": "是前置",
    "applies_to": "可用于",
    "contrasts_with": "可对照",
    "refines": "进一步细化",
    "part_of": "组成",
}


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _plan_payload(body) -> dict:
    return {
        "shelf_id": body.shelf_id,
        "topic": body.topic,
        "role": body.role,
        "experience": body.experience,
        "purpose": body.purpose,
        "depth": body.depth,
        "details": body.details,
    }


class LearningStartService:
    """Shows a reviewed graph slice and binds the user's explicit startup choice."""

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        ai,
        baselines,
        shelf_provider,
        profile_provider: Callable[[], dict],
    ):
        self.db = db
        self.user_id = user_id
        self.ai = ai
        self.baselines = baselines
        self.shelf_provider = shelf_provider
        self.profile_provider = profile_provider

    async def interview(self, body) -> dict:
        shelf = self.shelf_provider(body.shelf_id)
        profile = self.profile_provider()
        daily_hours = f"{body.daily_commitment_hours:g}"
        horizon_unit = {"day": "天", "week": "周", "month": "月"}[
            body.completion_horizon_unit
        ]
        daily_commitment = f"每天{daily_hours}小时"
        completion_horizon = (
            f"{body.completion_horizon_value}{horizon_unit}内"
        )
        request = {
            "topic": body.topic.strip(),
            "dailyCommitment": daily_commitment,
            "completionHorizon": completion_horizon,
            "schedule": {
                "dailyCommitmentHours": body.daily_commitment_hours,
                "completionHorizon": {
                    "value": body.completion_horizon_value,
                    "unit": body.completion_horizon_unit,
                },
            },
            "relatedExperience": body.related_experience.strip(),
            "answers": [item.model_dump(by_alias=True) for item in body.answers],
            "finalizationRequired": (
                len(body.answers) >= LEARNING_GOAL_INTERVIEW_MAX_ANSWERS
            ),
            "profile": profile,
            "shelf": {
                "name": shelf.name,
                "domain": shelf.domain,
                "specialty": shelf.specialty,
            },
            "interviewRuleVersion": LEARNING_GOAL_INTERVIEW_RULE_VERSION,
        }
        result = await self.ai.learning_goal_interview(request)
        if result.status == "ask":
            answered_question_ids = {
                item.question_id for item in body.answers
            }
            if result.question and result.question.id in answered_question_ids:
                raise AppError(
                    "这一轮信息没有继续推进，请重试整理目标确认稿",
                    code="LEARNING_GOAL_INTERVIEW_STALLED",
                    status=503,
                    retryable=True,
                )
            if len(body.answers) >= LEARNING_GOAL_INTERVIEW_MAX_ANSWERS:
                raise AppError(
                    "目标信息已经收集完成，但确认稿暂时没有整理好，请重试",
                    code="LEARNING_GOAL_INTERVIEW_FINALIZATION_INCOMPLETE",
                    status=503,
                    retryable=True,
                )
        question = None
        if result.question:
            question = {
                "id": result.question.id,
                "dimension": result.question.dimension,
                "prompt": result.question.prompt,
                "helper": result.question.helper,
                "options": [
                    {
                        "id": item.id,
                        "label": item.label,
                        "description": item.description,
                    }
                    for item in result.question.options
                ],
            }
        brief = None
        if result.brief:
            brief = {
                "topic": result.brief.topic,
                "purpose": result.brief.purpose,
                "successMarker": result.brief.success_marker,
                "startingPoint": result.brief.starting_point,
                "dailyCommitment": result.brief.daily_commitment,
                "completionHorizon": result.brief.completion_horizon,
                "scope": result.brief.scope,
                "outOfScope": result.brief.out_of_scope,
                "recommendedDepth": result.brief.recommended_depth,
            }
        return {
            "schemaVersion": result.schema_version,
            "status": result.status,
            "progressMessage": result.progress_message,
            "dimensions": [
                {
                    "key": item.key,
                    "status": item.status,
                    "summary": item.summary,
                    "confidence": item.confidence,
                }
                for item in result.dimensions
            ],
            "question": question,
            "brief": brief,
            "answerCount": len(body.answers),
            "generationMode": "ai" if getattr(self.ai, "configured", True) else "demo",
            "ruleVersion": LEARNING_GOAL_INTERVIEW_RULE_VERSION,
        }

    def preview(self, body) -> dict:
        shelf = self.shelf_provider(body.shelf_id)
        plan_input = _plan_payload(body)
        baseline = self.baselines.select_for_plan(
            shelf=shelf,
            plan_input=plan_input,
        )
        release = (
            self.db.scalar(
                select(KnowledgeGraphRelease)
                .where(
                    KnowledgeGraphRelease.baseline_version_id == baseline.id,
                    KnowledgeGraphRelease.status == "published",
                )
                .order_by(KnowledgeGraphRelease.version.desc())
            )
            if baseline
            else None
        )
        nodes: list[dict] = []
        edges: list[dict] = []
        if release:
            manifest = _load(release.manifest_json, {})
            concept_ids = list(manifest.get("conceptRevisionIds") or [])
            revisions = self.db.scalars(
                select(ConceptRevision)
                .where(
                    ConceptRevision.id.in_(concept_ids),
                    ConceptRevision.verification_status == "reviewed",
                )
                .order_by(ConceptRevision.label, ConceptRevision.id)
            ).all() if concept_ids else []
            visible_ids = {item.id for item in revisions}
            nodes = [
                {
                    "conceptRevisionId": item.id,
                    "label": item.label,
                    "meaning": item.definition,
                }
                for item in revisions
            ]
            relation_rows = self.db.scalars(
                select(ConceptRelationVersion)
                .where(
                    ConceptRelationVersion.release_id == release.id,
                    ConceptRelationVersion.status == "published",
                    ConceptRelationVersion.from_concept_revision_id.in_(visible_ids),
                    ConceptRelationVersion.to_concept_revision_id.in_(visible_ids),
                )
                .order_by(ConceptRelationVersion.relation_type, ConceptRelationVersion.id)
            ).all() if visible_ids else []
            edges = [
                {
                    "id": item.id,
                    "from": item.from_concept_revision_id,
                    "to": item.to_concept_revision_id,
                    "type": item.relation_type,
                    "label": RELATION_LABELS.get(item.relation_type, "相关"),
                }
                for item in relation_rows
            ]

        snapshot = {
            "topic": body.topic,
            "baselineVersionId": baseline.id if baseline else None,
            "knowledgeGraphReleaseId": release.id if release else None,
            "nodes": nodes,
            "edges": edges,
        }
        preview = LearningStartPreview(
            id=_uid("learning_start_preview"),
            user_id=self.user_id,
            shelf_id=body.shelf_id,
            knowledge_graph_release_id=release.id if release else None,
            topic=body.topic,
            request_hash=_hash(plan_input),
            visible_concept_revision_ids_json=_dump(
                [item["conceptRevisionId"] for item in nodes]
            ),
            snapshot_json=_dump(snapshot),
            schema_version=LEARNING_START_PREVIEW_SCHEMA_VERSION,
        )
        self.db.add(preview)
        self.db.commit()
        availability = "ready" if nodes else "not_ready"
        return {
            "schemaVersion": LEARNING_START_PREVIEW_SCHEMA_VERSION,
            "previewId": preview.id,
            "availability": availability,
            "topic": body.topic,
            "title": baseline.title if baseline else body.topic,
            "nodes": nodes,
            "edges": edges,
            "message": (
                "点亮你愿意投入时间的方向，未点亮的内容会降低优先级。"
                if nodes
                else "这个方向暂时没有可选择的知识版图，可以直接开始。"
            ),
        }

    def planning_context(self, body) -> dict:
        if body.start_mode == "direct":
            return {
                "mode": "direct",
                "selectedKnowledge": [],
                "deprioritizedKnowledge": [],
                "learningPreferences": [],
                "ruleVersion": LEARNING_START_SELECTION_RULE_VERSION,
            }
        selection = body.learning_start_selection
        preview = self.db.get(LearningStartPreview, selection.preview_id)
        if (
            not preview
            or preview.user_id != self.user_id
            or preview.shelf_id != body.shelf_id
        ):
            raise AppError(
                "知识版图选择已经失效，请重新打开后再选择",
                code="LEARNING_START_PREVIEW_NOT_FOUND",
                status=404,
            )
        if preview.request_hash != _hash(_plan_payload(body)):
            raise AppError(
                "学习方向已经改变，请重新选择感兴趣的内容",
                code="LEARNING_START_PREVIEW_MISMATCH",
                status=409,
            )
        visible_ids = set(
            _load(preview.visible_concept_revision_ids_json, [])
        )
        selected_ids = set(selection.selected_concept_revision_ids)
        if not selected_ids or not selected_ids.issubset(visible_ids):
            raise AppError(
                "点亮的知识方向不属于这次可选版图",
                code="LEARNING_START_SELECTION_OUT_OF_SCOPE",
                status=409,
            )
        snapshot = _load(preview.snapshot_json, {})
        nodes = {
            item["conceptRevisionId"]: item
            for item in snapshot.get("nodes", [])
        }
        return {
            "mode": "guided",
            "previewId": preview.id,
            "knowledgeGraphReleaseId": preview.knowledge_graph_release_id,
            "selectedKnowledge": [
                nodes[item_id]
                for item_id in selection.selected_concept_revision_ids
                if item_id in nodes
            ],
            "deprioritizedKnowledge": [
                item
                for item_id, item in nodes.items()
                if item_id not in selected_ids
            ],
            "learningPreferences": list(selection.learning_preferences),
            "ruleVersion": LEARNING_START_SELECTION_RULE_VERSION,
        }

    def bind_series(self, *, series_id: str, body) -> None:
        selection = body.learning_start_selection
        self.db.add(
            SeriesLearningStartPreference(
                id=_uid("series_learning_start"),
                user_id=self.user_id,
                series_id=series_id,
                preview_id=selection.preview_id if selection else None,
                start_mode=body.start_mode,
                selected_concept_revision_ids_json=_dump(
                    selection.selected_concept_revision_ids if selection else []
                ),
                learning_preferences_json=_dump(
                    selection.learning_preferences if selection else []
                ),
                rule_version=LEARNING_START_SELECTION_RULE_VERSION,
            )
        )

    @staticmethod
    def plan_payload(body) -> dict:
        return _plan_payload(body)


class ChapterChoiceService:
    """Owns chapter skip/resume facts and governed chapter challenge evidence."""

    def __init__(self, db: Session, *, user_id: str, contexts, progress):
        self.db = db
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress

    def challenge_view(self, chapter_id: str) -> dict:
        context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        chapter_progress = self.progress.for_chapter(
            context.chapter, context.book
        )
        if chapter_progress.status == "locked":
            raise AppError("本章尚未开启", code="CHAPTER_LOCKED", status=403)
        if chapter_progress.status == "skipped":
            raise AppError(
                "请先把本章重新加入学习",
                code="CHAPTER_SKIPPED",
                status=409,
            )
        if chapter_progress.status == "completed":
            raise AppError(
                "本章已经完成",
                code="CHAPTER_ALREADY_COMPLETED",
                status=409,
            )
        sections = self.db.scalars(
            select(Section)
            .where(Section.chapter_id == chapter_id)
            .order_by(Section.position)
        ).all()
        if not sections:
            raise AppError(
                "章挑战还没有准备好",
                code="CHAPTER_CHALLENGE_NOT_PREPARED",
                status=409,
            )
        run = self.progress.active_run(context.series.id)
        section_views = []
        for section in sections:
            section_progress = self.progress.for_section(
                section, context.chapter, context.book
            )
            if section_progress.status == "completed":
                continue
            binding = self.db.scalar(
                select(LearningRunSectionBinding).where(
                    LearningRunSectionBinding.learning_run_id == run.id,
                    LearningRunSectionBinding.user_id == self.user_id,
                    LearningRunSectionBinding.section_id == section.id,
                )
            )
            quiz = (
                self.db.get(QuizSet, binding.initial_quiz_set_id)
                if binding and binding.initial_quiz_set_id
                else None
            )
            governance = governance_view_for_quiz(
                self.db, quiz.id if quiz else None
            )
            if (
                not binding
                or not quiz
                or quiz.publication_status != "published"
                or not governance
                or not governance["allowed"]
                or not governance["assessmentEligible"]
            ):
                raise AppError(
                    "章挑战的验证题还没有完整准备好",
                    code="CHAPTER_CHALLENGE_QUIZ_UNAVAILABLE",
                    status=409,
                )
            questions = immutable_questions_for_quiz(
                self.db,
                quiz,
                require_versions=False,
                require_evidence=True,
            )
            section_views.append(
                {
                    "sectionId": section.id,
                    "position": section.position,
                    "title": section.title,
                    "quizSetId": quiz.id,
                    "questions": self._public_questions(questions),
                }
            )
        return {
            "schemaVersion": "chapter_challenge_view_v1",
            "chapterId": context.chapter.id,
            "chapterTitle": context.chapter.title,
            "objective": context.chapter.objective,
            "status": "ready",
            "questionCount": sum(
                len(item["questions"]) for item in section_views
            ),
            "sections": section_views,
        }

    def submit_challenge(self, chapter_id: str, body, idempotency_key: str) -> dict:
        request_key = (idempotency_key or "").strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "章挑战请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        run = self.progress.active_run(context.series.id)
        request_payload = body.model_dump(by_alias=False)
        request_hash = _hash(request_payload)
        replay = self.db.scalar(
            select(ChapterChallengeAttempt).where(
                ChapterChallengeAttempt.learning_run_id == run.id,
                ChapterChallengeAttempt.user_id == self.user_id,
                ChapterChallengeAttempt.idempotency_key == request_key,
            )
        )
        if replay:
            if replay.request_hash != request_hash:
                raise AppError(
                    "章挑战请求标识已用于其他答案",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            if replay.status == "completed":
                return _load(replay.response_json, {})
            raise AppError(
                "相同章挑战正在评分",
                code="CHAPTER_CHALLENGE_IN_PROGRESS",
                status=409,
            )

        challenge = self.challenge_view(chapter_id)
        expected = {
            item["sectionId"]: item for item in challenge["sections"]
        }
        submitted = {item.section_id: item for item in body.sections}
        if set(expected) != set(submitted):
            raise AppError(
                "章挑战必须完成当前全部小节的验证题",
                code="CHAPTER_CHALLENGE_COVERAGE_INCOMPLETE",
                status=409,
            )
        aggregate = ChapterChallengeAttempt(
            id=_uid("chapter_challenge"),
            learning_run_id=run.id,
            user_id=self.user_id,
            chapter_id=chapter_id,
            idempotency_key=request_key,
            request_hash=request_hash,
            request_json=_dump(request_payload),
            status="processing",
            rule_version=CHAPTER_CHALLENGE_RULE_VERSION,
        )
        self.db.add(aggregate)
        self.db.flush()

        section_results = []
        compatibility = SubmitQuiz(self.db, user_id=self.user_id)
        for section_id, expected_item in expected.items():
            submission = submitted[section_id]
            if submission.quiz_set_id != expected_item["quizSetId"]:
                raise AppError(
                    "章挑战题目版本已经变化，请重新开始",
                    code="CHAPTER_CHALLENGE_QUIZ_MISMATCH",
                    status=409,
                )
            section_context = self.contexts.resolve_section(
                user_id=self.user_id,
                section_id=section_id,
            )
            quiz = self.db.get(QuizSet, submission.quiz_set_id)
            binding = self.db.scalar(
                select(LearningRunSectionBinding).where(
                    LearningRunSectionBinding.learning_run_id == run.id,
                    LearningRunSectionBinding.user_id == self.user_id,
                    LearningRunSectionBinding.section_id == section_id,
                )
            )
            if not quiz or not binding or quiz.id != binding.initial_quiz_set_id:
                raise AppError(
                    "章挑战题集不属于当前学习实例",
                    code="CHAPTER_CHALLENGE_QUIZ_MISMATCH",
                    status=409,
                )
            questions = immutable_questions_for_quiz(
                self.db,
                quiz,
                require_versions=False,
                require_evidence=True,
            )
            grade = grade_choice_quiz(questions, submission.answers)
            previous_attempt = self.db.scalar(
                select(QuizAttempt).where(
                    QuizAttempt.learning_run_id == run.id,
                    QuizAttempt.user_id == self.user_id,
                    QuizAttempt.quiz_set_id == quiz.id,
                )
            )
            attempt = QuizAttempt(
                id=_uid("attempt"),
                quiz_set_id=quiz.id,
                learning_contract_version_id=binding.learning_contract_version_id,
                content_version_id=binding.content_version_id,
                learning_run_id=run.id,
                user_id=self.user_id,
                idempotency_key=f"challenge:{aggregate.id}:{section_id}"[:128],
                request_hash=_hash(
                    {
                        "chapterChallengeId": aggregate.id,
                        "sectionId": section_id,
                        "quizSetId": quiz.id,
                        "answers": submission.answers,
                    }
                ),
                answers_json=_dump(submission.answers),
                results_json=_dump(grade.results),
                passed=grade.passed,
            )
            self.db.add(attempt)
            self.db.flush()
            target_ids = {
                str(item.get("assessmentTargetId") or "")
                for item in questions
                if item.get("assessmentTargetId")
            }
            capability_before = capability_state_views_for_targets(
                self.db,
                user_id=self.user_id,
                target_ids=target_ids,
            )
            record_scoring_facts(
                self.db,
                attempt=attempt,
                section=section_context.section,
                questions=questions,
                results=grade.results,
                score=grade.score,
                total=grade.total,
                passed=grade.passed,
                assistance_mode=(
                    "unassisted_repeat" if previous_attempt else "unassisted_initial"
                ),
                learning_episode_id=(
                    f"chapter_challenge:{aggregate.id}:{section_id}"
                ),
                source_type="chapter_challenge",
            )
            settlement = append_capability_settlement_snapshot(
                self.db,
                attempt=attempt,
                section_id=section_id,
                target_ids=target_ids,
                before=capability_before,
                trigger_kind="chapter_challenge",
            )
            gate = section_gate_decision(
                self.db,
                learning_run_id=run.id,
                section_id=section_id,
            )
            append_assessment_gate_snapshot(
                self.db,
                attempt=attempt,
                section_id=section_id,
                decision=gate,
                trigger_kind="chapter_challenge",
            )
            progress = self.progress.for_section(
                section_context.section,
                section_context.chapter,
                section_context.book,
            )
            progress.best_score, progress.total_score = best_score_pair(
                progress.best_score,
                progress.total_score,
                grade.score,
                grade.total,
            )
            progress.ask_me_unlocked |= grade.perfect
            progress.version += 1
            progress.updated_at = now()
            progress.status = "completed" if gate.passed else "available"
            attempt.passed = gate.passed
            compatibility._record_evidence(
                section_context,
                questions,
                grade.results,
                attempt.id,
                update_legacy_memory=not bool(quiz.learning_contract_version_id),
            )
            section_result = {
                "sectionId": section_id,
                "position": expected_item["position"],
                "title": expected_item["title"],
                "status": "passed" if gate.passed else "needs_learning",
                "score": grade.score,
                "total": grade.total,
                "results": grade.results,
                "knowledgeSettlement": settlement,
            }
            attempt.workflow_status = "completed"
            attempt.response_json = _dump(section_result)
            section_results.append(section_result)

        passed = all(item["status"] == "passed" for item in section_results)
        next_route = {}
        if passed:
            self.progress.set_status(
                self.progress.for_chapter(context.chapter, context.book),
                "completed",
            )
            next_route = self._unlock_after_chapter(context)
        else:
            self.progress.set_status(
                self.progress.for_chapter(context.chapter, context.book),
                "available",
            )
        response = {
            "schemaVersion": "chapter_challenge_result_v1",
            "attemptId": aggregate.id,
            "chapterId": chapter_id,
            "passed": passed,
            "passedSectionCount": sum(
                item["status"] == "passed" for item in section_results
            ),
            "totalSectionCount": len(section_results),
            "sectionResults": section_results,
            **next_route,
        }
        aggregate.passed = passed
        aggregate.status = "completed"
        aggregate.response_json = _dump(response)
        self.db.add(
            ChapterRouteDecisionEvent(
                id=_uid("chapter_route"),
                learning_run_id=run.id,
                user_id=self.user_id,
                chapter_id=chapter_id,
                action="challenge",
                reason="passed" if passed else "needs_learning",
                source="chapter_entry",
                idempotency_key=f"challenge:{aggregate.id}"[:128],
                outcome_json=_dump(response),
                rule_version=CHAPTER_ROUTE_RULE_VERSION,
            )
        )
        self.db.commit()
        return response

    def skip(self, chapter_id: str, body, idempotency_key: str) -> dict:
        request_key = (idempotency_key or "").strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "跳过请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        run = self.progress.active_run(context.series.id)
        replay = self.db.scalar(
            select(ChapterRouteDecisionEvent).where(
                ChapterRouteDecisionEvent.learning_run_id == run.id,
                ChapterRouteDecisionEvent.user_id == self.user_id,
                ChapterRouteDecisionEvent.idempotency_key == request_key,
            )
        )
        if replay:
            if replay.chapter_id != chapter_id or replay.action != "skip":
                raise AppError(
                    "跳过请求标识已用于其他操作",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            return _load(replay.outcome_json, {})
        chapter_progress = self.progress.for_chapter(
            context.chapter, context.book
        )
        if chapter_progress.status == "locked":
            raise AppError("本章尚未开启", code="CHAPTER_LOCKED", status=403)
        if chapter_progress.status == "completed":
            raise AppError(
                "已经完成的章节不需要跳过",
                code="CHAPTER_ALREADY_COMPLETED",
                status=409,
            )
        sections = self.db.scalars(
            select(Section).where(Section.chapter_id == chapter_id)
        ).all()
        for section in sections:
            progress = self.progress.for_section(
                section, context.chapter, context.book
            )
            if progress.status != "completed":
                self.progress.set_status(progress, "skipped")
        self.progress.set_status(chapter_progress, "skipped")
        next_route = self._unlock_after_chapter(context)
        outcome = {
            "chapterId": chapter_id,
            "status": "skipped",
            "reason": body.reason,
            **next_route,
        }
        self.db.add(
            ChapterRouteDecisionEvent(
                id=_uid("chapter_route"),
                learning_run_id=run.id,
                user_id=self.user_id,
                chapter_id=chapter_id,
                action="skip",
                reason=body.reason,
                source=(
                    "challenge_exit"
                    if body.reason == "challenge_exit"
                    else "chapter_entry"
                ),
                idempotency_key=request_key,
                outcome_json=_dump(outcome),
                rule_version=CHAPTER_ROUTE_RULE_VERSION,
            )
        )
        self.db.commit()
        return outcome

    def resume(self, chapter_id: str, idempotency_key: str) -> dict:
        request_key = (idempotency_key or "").strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "重新学习请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        run = self.progress.active_run(context.series.id)
        replay = self.db.scalar(
            select(ChapterRouteDecisionEvent).where(
                ChapterRouteDecisionEvent.learning_run_id == run.id,
                ChapterRouteDecisionEvent.user_id == self.user_id,
                ChapterRouteDecisionEvent.idempotency_key == request_key,
            )
        )
        if replay:
            if replay.chapter_id != chapter_id or replay.action != "resume":
                raise AppError(
                    "重新学习请求标识已用于其他操作",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            return _load(replay.outcome_json, {})
        chapter_progress = self.progress.for_chapter(
            context.chapter, context.book
        )
        if chapter_progress.status == "locked":
            raise AppError("本章尚未开启", code="CHAPTER_LOCKED", status=403)
        if chapter_progress.status == "skipped":
            self.progress.set_status(chapter_progress, "available")
            book_progress = self.progress.for_book(context.book)
            if book_progress.status == "completed":
                self.progress.set_status(book_progress, "available")
            sections = self.db.scalars(
                select(Section)
                .where(Section.chapter_id == chapter_id)
                .order_by(Section.position)
            ).all()
            first_pending = True
            for section in sections:
                progress = self.progress.for_section(
                    section, context.chapter, context.book
                )
                if progress.status == "completed":
                    continue
                self.progress.set_status(
                    progress,
                    "available" if first_pending else "locked",
                )
                first_pending = False
        outcome = {"chapterId": chapter_id, "status": chapter_progress.status}
        self.db.add(
            ChapterRouteDecisionEvent(
                id=_uid("chapter_route"),
                learning_run_id=run.id,
                user_id=self.user_id,
                chapter_id=chapter_id,
                action="resume",
                reason="",
                source="chapter_entry",
                idempotency_key=request_key,
                outcome_json=_dump(outcome),
                rule_version=CHAPTER_ROUTE_RULE_VERSION,
            )
        )
        self.db.commit()
        return outcome

    def _unlock_after_chapter(self, context) -> dict:
        run = self.progress.active_run(context.series.id)
        later_rows = self.db.execute(
            select(Chapter, ChapterProgress)
            .join(
                ChapterProgress,
                ChapterProgress.chapter_id == Chapter.id,
            )
            .where(
                Chapter.book_id == context.book.id,
                Chapter.position > context.chapter.position,
                ChapterProgress.learning_run_id == run.id,
            )
            .order_by(Chapter.position)
        ).all()
        next_chapter = next(
            (
                (chapter, progress)
                for chapter, progress in later_rows
                if progress.status not in {"completed", "skipped"}
            ),
            None,
        )
        if next_chapter:
            chapter, progress = next_chapter
            if progress.status == "locked":
                self.progress.set_status(progress, "available")
            first_section = self.db.scalar(
                select(Section)
                .where(Section.chapter_id == chapter.id)
                .order_by(Section.position)
            )
            if first_section:
                section_progress = self.progress.for_section(
                    first_section, chapter, context.book
                )
                if section_progress.status == "locked":
                    self.progress.set_status(section_progress, "available")
            return {"nextChapterId": chapter.id, "nextBookId": None}

        self.progress.set_status(self.progress.for_book(context.book), "completed")
        next_book_row = self.db.execute(
            select(Book, BookProgress)
            .join(BookProgress, BookProgress.book_id == Book.id)
            .where(
                Book.series_id == context.series.id,
                Book.position > context.book.position,
                Book.deleted_at.is_(None),
                BookProgress.learning_run_id == run.id,
            )
            .order_by(Book.position)
        ).first()
        if not next_book_row:
            return {"nextChapterId": None, "nextBookId": None}
        next_book, next_book_progress = next_book_row
        if next_book.outline_status != "confirmed":
            return {"nextChapterId": None, "nextBookId": next_book.id}
        if next_book_progress.status == "locked":
            self.progress.set_status(next_book_progress, "available")
        first_chapter = self.db.scalar(
            select(Chapter)
            .where(Chapter.book_id == next_book.id)
            .order_by(Chapter.position)
        )
        if first_chapter:
            first_chapter_progress = self.progress.for_chapter(
                first_chapter, next_book
            )
            if first_chapter_progress.status == "locked":
                self.progress.set_status(first_chapter_progress, "available")
            first_section = self.db.scalar(
                select(Section)
                .where(Section.chapter_id == first_chapter.id)
                .order_by(Section.position)
            )
            if first_section:
                first_section_progress = self.progress.for_section(
                    first_section, first_chapter, next_book
                )
                if first_section_progress.status == "locked":
                    self.progress.set_status(first_section_progress, "available")
        return {
            "nextChapterId": first_chapter.id if first_chapter else None,
            "nextBookId": next_book.id,
        }

    @staticmethod
    def _public_questions(items: list[dict]) -> list[dict]:
        hidden = {
            "correct",
            "explanation",
            "claim_block_indexes",
            "assessmentTargetId",
            "equivalenceGroupId",
            "itemKey",
        }
        return [
            {
                **{key: value for key, value in item.items() if key not in hidden},
                "selectionMode": (
                    "multiple"
                    if len(set(item.get("correct", []))) > 1
                    else "single"
                ),
            }
            for item in items
        ]
