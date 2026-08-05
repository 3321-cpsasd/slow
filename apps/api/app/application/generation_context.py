import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.context import (
    ContextLineage,
    ContextOperation,
    CurriculumContext,
    GenerationContextPack,
    LearnerContext,
    LearningStateContext,
    MissionContext,
    policy_for,
)
from ..core.errors import AppError
from ..infrastructure.tables import (
    AssessmentObservation,
    AssessmentTarget,
    Book,
    Chapter,
    LearningContractVersion,
    LearningContractAssessmentTarget,
    LearningMissionVersion,
    LearningPlan,
    QuizAttempt,
    Section,
    Series,
    Shelf,
    UserProfile,
)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class GenerationContextBuilder:
    """The sole builder for materialized, task-scoped AI context."""

    def __init__(self, db: Session, *, user_id: str):
        self.db = db
        self.user_id = user_id

    def build(
        self,
        operation: ContextOperation,
        *,
        shelf: Shelf,
        memory: list[dict],
        series: Series | None = None,
        book: Book | None = None,
        chapter: Chapter | None = None,
        section: Section | None = None,
        mission: LearningMissionVersion | None = None,
        contract: LearningContractVersion | None = None,
        plan_input: dict[str, Any] | None = None,
        attempt: QuizAttempt | None = None,
        feedback: dict[str, Any] | None = None,
        interaction: dict[str, Any] | None = None,
    ) -> GenerationContextPack:
        plan = self._plan(series, mission)
        learner = self._learner(plan, mission, plan_input)
        mission_depth = (
            _load(mission.constraints_json, {}).get("depth") if mission else ""
        )
        depth = str(
            (plan_input or {}).get("depth")
            or (plan.depth if plan else "")
            or mission_depth
            or "deep"
        )
        if depth not in {"overview", "deep", "mastery"}:
            depth = "deep"
        mission_context = self._mission(mission)
        curriculum = self._curriculum(
            shelf=shelf,
            series=series,
            book=book,
            chapter=chapter,
            section=section,
        )
        evidence_watermark = self.db.scalar(
            select(func.max(AssessmentObservation.sequence)).where(
                AssessmentObservation.user_id == self.user_id
            )
        ) or 0
        learning_state = LearningStateContext(
            relevantMemory=memory,
            evidenceWatermark=evidence_watermark,
            attempt=self._attempt(attempt),
            feedback=feedback or {},
        )
        policy = policy_for(operation, depth)
        pack = GenerationContextPack(
            operation=operation,
            learner=learner,
            mission=mission_context,
            learningContract=self._contract(contract),
            curriculum=curriculum,
            learningState=learning_state,
            interaction=interaction or {},
            policy=policy,
            lineage=ContextLineage(
                profileVersion=learner.profile_version,
                missionVersionId=mission.id if mission else "",
                contractVersionId=contract.id if contract else "",
                evidenceWatermark=evidence_watermark,
                policyVersion=policy.version,
            ),
        )
        self._validate(
            pack,
            shelf=shelf,
            series=series,
            book=book,
            chapter=chapter,
            section=section,
            mission=mission,
            contract=contract,
        )
        return pack

    @staticmethod
    def attach(payload: dict[str, Any], pack: GenerationContextPack) -> dict[str, Any]:
        return {**payload, "generationContext": pack.payload()}

    def _plan(
        self,
        series: Series | None,
        mission: LearningMissionVersion | None,
    ) -> LearningPlan | None:
        plan_id = series.plan_id if series else mission.plan_id if mission else None
        return self.db.get(LearningPlan, plan_id) if plan_id else None

    def _learner(
        self,
        plan: LearningPlan | None,
        mission: LearningMissionVersion | None,
        plan_input: dict[str, Any] | None,
    ) -> LearnerContext:
        profile = self.db.get(UserProfile, self.user_id)
        baseline = {
            "profileVersion": profile.version if profile else 0,
            "profession": profile.profession if profile else "",
            "stage": profile.stage if profile else "",
            "purpose": profile.purpose if profile else "",
            "domains": _load(profile.domains_json, []) if profile else [],
            "experience": profile.experience if profile else "",
            "weeklyMinutes": profile.weekly_minutes if profile else 0,
            "targetDate": profile.target_date if profile else "",
        }
        adopted = _load(mission.learner_context_json, {}) if mission else {}
        submitted = plan_input or {}
        plan_role = str(
            submitted.get("role")
            or adopted.get("role")
            or (plan.role if plan else "")
        ).strip()
        plan_experience = str(
            submitted.get("experience")
            or adopted.get("experience")
            or (plan.experience if plan else "")
        ).strip()
        for key, alias in (
            ("profileVersion", "profileVersion"),
            ("profession", "profession"),
            ("stage", "stage"),
            ("purpose", "purpose"),
            ("domains", "domains"),
            ("experience", "experience"),
            ("weeklyMinutes", "weeklyMinutes"),
            ("targetDate", "targetDate"),
        ):
            if adopted.get(key) not in (None, "", []):
                baseline[alias] = adopted[key]
        if plan and plan.purpose:
            baseline["purpose"] = plan.purpose
        if submitted.get("purpose"):
            baseline["purpose"] = submitted["purpose"]
        baseline["planRole"] = plan_role
        baseline["planExperience"] = plan_experience
        baseline["provenance"] = (
            "mission_snapshot" if mission else "confirmed_profile_and_plan_input"
        )
        return LearnerContext.model_validate(baseline)

    @staticmethod
    def _mission(mission: LearningMissionVersion | None) -> MissionContext | None:
        if not mission:
            return None
        return MissionContext(
            versionId=mission.id,
            version=mission.version,
            why=mission.why,
            targetCapabilities=_load(mission.target_capabilities_json, []),
            constraints=_load(mission.constraints_json, {}),
            outOfScope=_load(mission.out_of_scope_json, []),
            assumptions=_load(mission.assumptions_json, []),
            learnerContext=_load(mission.learner_context_json, {}),
            status=mission.status,
        )

    def _contract(
        self,
        contract: LearningContractVersion | None,
    ) -> dict[str, Any]:
        if not contract:
            return {}
        target_rows = self.db.execute(
            select(LearningContractAssessmentTarget, AssessmentTarget)
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == LearningContractAssessmentTarget.assessment_target_id,
            )
            .where(
                LearningContractAssessmentTarget.contract_version_id
                == contract.id
            )
            .order_by(LearningContractAssessmentTarget.position)
        ).all()
        return {
            "id": contract.id,
            "version": contract.version,
            "missionVersionId": contract.mission_version_id,
            "sectionQuestion": contract.section_question_snapshot,
            "targetDepth": contract.target_depth,
            "boundaries": _load(contract.boundaries_json, []),
            "generationContext": _load(contract.generation_context_json, {}),
            "targets": [
                {
                    "assessmentTargetId": target.id,
                    "objective": target.objective_statement,
                    "dimension": target.dimension,
                    "targetDepth": contract.target_depth,
                    "assessmentLevel": target.target_depth,
                    "required": binding.required,
                    "verificationPolicy": binding.verification_policy,
                    "diagnosticOnly": binding.diagnostic_only,
                }
                for binding, target in target_rows
            ],
        }

    def _curriculum(
        self,
        *,
        shelf: Shelf,
        series: Series | None,
        book: Book | None,
        chapter: Chapter | None,
        section: Section | None,
    ) -> CurriculumContext:
        chapters = (
            self.db.scalars(
                select(Chapter)
                .where(Chapter.book_id == book.id)
                .order_by(Chapter.position)
            ).all()
            if book
            else []
        )
        sections = (
            self.db.scalars(
                select(Section)
                .where(Section.chapter_id == chapter.id)
                .order_by(Section.position)
            ).all()
            if chapter
            else []
        )
        return CurriculumContext(
            shelf={
                "id": shelf.id,
                "name": shelf.name,
                "domain": shelf.domain,
                "specialty": shelf.specialty,
                "tags": _load(shelf.tags_json, []),
            },
            series=(
                {"id": series.id, "title": series.title, "rationale": series.rationale}
                if series
                else {}
            ),
            book=(
                {
                    "id": book.id,
                    "position": book.position,
                    "title": book.title,
                    "topic": book.topic,
                    "description": book.description,
                    "estimatedMinutes": book.estimated_minutes,
                }
                if book
                else {}
            ),
            chapter=(
                {
                    "id": chapter.id,
                    "position": chapter.position,
                    "title": chapter.title,
                    "objective": chapter.objective,
                }
                if chapter
                else {}
            ),
            section=(
                {
                    "id": section.id,
                    "position": section.position,
                    "title": section.title,
                    "question": section.question,
                    "objectives": _load(section.objectives_json, []),
                }
                if section
                else {}
            ),
            neighboringChapters=[
                {
                    "id": item.id,
                    "position": item.position,
                    "title": item.title,
                    "objective": item.objective,
                    "current": bool(chapter and item.id == chapter.id),
                }
                for item in chapters
            ],
            neighboringSections=[
                {
                    "id": item.id,
                    "position": item.position,
                    "title": item.title,
                    "question": item.question,
                    "objectives": _load(item.objectives_json, []),
                    "current": bool(section and item.id == section.id),
                }
                for item in sections
            ],
        )

    @staticmethod
    def _attempt(attempt: QuizAttempt | None) -> dict[str, Any]:
        if not attempt:
            return {}
        return {
            "attemptId": attempt.id,
            "quizSetId": attempt.quiz_set_id,
            "answers": _load(attempt.answers_json, []),
            "scoringResults": _load(attempt.results_json, []),
            "passed": attempt.passed,
            "createdAt": attempt.created_at.isoformat(),
        }

    def _validate(
        self,
        pack: GenerationContextPack,
        *,
        shelf: Shelf,
        series: Series | None,
        book: Book | None,
        chapter: Chapter | None,
        section: Section | None,
        mission: LearningMissionVersion | None,
        contract: LearningContractVersion | None,
    ) -> None:
        if shelf.user_id != self.user_id:
            raise AppError(
                "书架不属于当前用户",
                code="GENERATION_CONTEXT_OWNER_MISMATCH",
                status=403,
            )
        if mission and mission.user_id != self.user_id:
            raise AppError(
                "学习使命不属于当前用户",
                code="GENERATION_CONTEXT_OWNER_MISMATCH",
                status=403,
            )
        if series and mission and series.plan_id != mission.plan_id:
            raise AppError(
                "学习使命与教材计划不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        if series and series.shelf_id != shelf.id:
            raise AppError(
                "教材系列与当前书架不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        if book and (
            book.shelf_id != shelf.id
            or (series is not None and book.series_id != series.id)
        ):
            raise AppError(
                "书与当前教材系列不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        if chapter and book and chapter.book_id != book.id:
            raise AppError(
                "章与当前书不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        if section and chapter and section.chapter_id != chapter.id:
            raise AppError(
                "小节与当前章不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        if contract and section and contract.section_id != section.id:
            raise AppError(
                "学习契约与当前小节不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        if contract and mission and contract.mission_version_id != mission.id:
            raise AppError(
                "学习契约与当前使命版本不一致",
                code="GENERATION_CONTEXT_LINEAGE_MISMATCH",
                status=409,
            )
        required = set(pack.policy.required_categories)
        if "learner" in required and not (
            pack.learner.plan_role or pack.learner.profession
        ):
            raise AppError(
                "生成缺少已确认的学习者身份上下文",
                code="GENERATION_LEARNER_CONTEXT_MISSING",
                status=409,
            )
        if "mission" in required and not pack.mission:
            raise AppError(
                "生成缺少已采用的学习使命",
                code="GENERATION_MISSION_CONTEXT_MISSING",
                status=409,
            )
        if "contract" in required and not contract:
            raise AppError(
                "生成缺少学习契约",
                code="GENERATION_CONTRACT_CONTEXT_MISSING",
                status=409,
            )
        if pack.operation == "remediation" and not pack.learning_state.attempt:
            raise AppError(
                "补救生成缺少原始作答与判分上下文",
                code="GENERATION_ATTEMPT_CONTEXT_MISSING",
                status=409,
            )
