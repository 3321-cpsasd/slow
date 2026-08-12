"""Isolated legacy section generation and remediation pipeline.

Imported lazily by SectionGenerationCoordinator so the v2 default route has no
dependency on this compatibility pipeline.
"""

from asyncio import CancelledError
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlparse

from sqlalchemy import func, select

from ..ai.contracts import (
    GeneratedLesson,
    GeneratedQuiz,
    GeneratedRemediationLesson,
)
from ..auth.context import WorkerExecutionContext
from ..core.errors import AiError, AppError
from ..infrastructure.tables import (
    ContentBlockVersion,
    ContentVersion,
    GenerationRun,
    LearningContractVersion,
    LearningMissionVersion,
    LearningRunSectionBinding,
    QuizAttempt,
    QuizSet,
    Remediation,
    SourceVerification,
    now,
)
from ..modules.learning.assessment import (
    assessment_contract_view,
    bind_questions_to_targets,
    failed_target_ids_for_attempt,
)
from ..modules.learning.assessment_items import (
    VERSIONED_LEGACY_QUIZ_SCHEMA,
    immutable_questions_for_quiz,
    publish_assessment_item_versions,
)
from ..modules.learning.contracts import ensure_learning_contract, open_run_section
from ..modules.learning.remediation_diagnosis import (
    choose_remediation_strategy,
    diagnose_failed_attempt,
)
from ..modules.learning.content_governance_store import (
    generated_claim_verification_candidates,
    persist_generated_governance,
    record_verified_claim_binding,
    reevaluate_generated_governance,
)
from ..services.source_verifier import SourceVerificationError
from .remediation_generation import publish_remediation_candidate
from .section_generation import (
    apply_source_repair_scope,
    assert_lesson_content_quality,
    attach_content_compliance_metadata,
    degraded_source_verification_report,
    dump,
    load,
    model_only_content,
    source_blacklist_from_generation_traces,
    timestamp,
    uid,
)


async def generate_legacy_section(
    self,
    section_id,
    retry=False,
    retry_attempt_id=None,
    regenerate=False,
    supersede_remediation_id=None,
    regeneration_feedback=None,
    resource_key=None,
    owner_id=None,
):
    if not retry:
        if callable(getattr(self.ai, "generate_lesson", None)):
            return await self._generate_section_v2(
                section_id,
                regenerate=regenerate,
                regeneration_feedback=regeneration_feedback,
                resource_key=resource_key,
                owner_id=owner_id,
            )
        if not getattr(
            self.ai,
            "allow_legacy_lesson_generation_for_tests",
            False,
        ):
            raise AppError(
                "当前 AI 适配器不支持版本化正文生成；拒绝回退旧链路",
                code="LESSON_GENERATION_V2_UNSUPPORTED",
                status=500,
            )
    section_context = self.contexts.resolve_section(
        user_id=self.user_id,
        section_id=section_id,
    )
    section = section_context.section
    learning_run = self.progress.active_run(section_context.series.id)
    mission_version = self.missions.current_version(section_context.series.id)
    contract = None
    failed_attempt_for_contract = None
    failed_quiz_for_contract = None
    if regeneration_feedback:
        feedback_content = self.db.get(
            ContentVersion,
            regeneration_feedback.get("contentVersionId"),
        )
        if (
            feedback_content
            and feedback_content.section_id == section.id
            and feedback_content.learning_contract_version_id
        ):
            contract = self.db.get(
                LearningContractVersion,
                feedback_content.learning_contract_version_id,
            )
            if contract:
                mission_version = self.db.get(
                    LearningMissionVersion,
                    contract.mission_version_id,
                )
        if not contract or not mission_version:
            raise AppError(
                "反馈对应的正文缺少可追溯学习契约，不能安全重新生成",
                code="FEEDBACK_CONTRACT_MISSING",
                status=409,
            )
    if retry and retry_attempt_id:
        failed_attempt_for_contract = self.db.get(
            QuizAttempt, retry_attempt_id
        )
        failed_quiz_for_contract = (
            self.db.get(QuizSet, failed_attempt_for_contract.quiz_set_id)
            if failed_attempt_for_contract
            else None
        )
        if (
            failed_quiz_for_contract
            and failed_quiz_for_contract.learning_contract_version_id
        ):
            contract = self.db.get(
                LearningContractVersion,
                failed_quiz_for_contract.learning_contract_version_id,
            )
            mission_version = self.db.get(
                LearningMissionVersion, contract.mission_version_id
            )
    if regenerate and not regeneration_feedback:
        active_binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        bound_contract = (
            self.db.get(
                LearningContractVersion,
                active_binding.learning_contract_version_id,
            )
            if active_binding
            else None
        )
        if (
            bound_contract
            and bound_contract.section_id == section.id
            and bound_contract.mission_version_id == mission_version.id
        ):
            contract = bound_contract
    contract = contract or ensure_learning_contract(
        self.db,
        section,
        mission_version_id=mission_version.id,
        provenance_mode="native_m2",
    )
    superseded_remediation = None
    if supersede_remediation_id:
        if not retry or not retry_attempt_id:
            raise AppError(
                "补救内容再生成必须绑定原补救记录和答题记录",
                code="REMEDIATION_REGENERATION_SCOPE_INVALID",
                status=400,
            )
        superseded_remediation = self.db.get(
            Remediation,
            supersede_remediation_id,
        )
        if (
            not superseded_remediation
            or superseded_remediation.section_id != section.id
            or superseded_remediation.attempt_id != retry_attempt_id
        ):
            raise AppError(
                "原补救记录不属于当前小节或答题",
                code="REMEDIATION_REGENERATION_SCOPE_INVALID",
                status=409,
            )
        if self.db.scalar(
            select(Remediation).where(
                Remediation.supersedes_id == superseded_remediation.id
            )
        ):
            raise AppError(
                "该补救内容已有更新版本",
                code="REMEDIATION_ALREADY_SUPERSEDED",
                status=409,
            )
    section_progress = self.progress.for_section(
        section,
        section_context.chapter,
        section_context.book,
    )
    if (
        section_progress.status == "locked"
        and not isinstance(self.scope, WorkerExecutionContext)
    ):
        raise AppError("小节未解锁", code="SECTION_LOCKED", status=403)
    if (
        section_progress.status == "preparing"
        and not isinstance(self.scope, WorkerExecutionContext)
    ):
        raise AppError(
            "下一节正文和验证题仍在准备中",
            code="SECTION_PREPARING",
            status=409,
        )
    existing = self.db.scalar(
        select(ContentVersion)
        .where(
            ContentVersion.section_id == section.id,
            ContentVersion.learning_contract_version_id == contract.id,
        )
        .order_by(ContentVersion.version.desc())
    )
    if retry and failed_quiz_for_contract:
        existing = self.db.get(
            ContentVersion,
            failed_quiz_for_contract.content_version_id,
        )
    latest_quiz = self.db.scalar(
        select(QuizSet)
        .where(
            QuizSet.section_id == section.id,
            QuizSet.learning_contract_version_id == contract.id,
        )
        .order_by(QuizSet.generation.desc())
    )
    if existing and not retry and not regenerate:
        return self.section(section.id)
    if regenerate:
        if not existing:
            raise AppError(
                "本节还没有正文，请直接生成",
                code="SECTION_CONTENT_MISSING",
                status=409,
            )
        if not latest_quiz or latest_quiz.content_version_id != existing.id:
            raise AppError(
                "当前正文缺少匹配的验证题，不能安全重新生成",
                code="SECTION_QUIZ_MISSING",
                status=409,
            )
        assessed = self.db.scalar(
            select(func.count())
            .select_from(QuizAttempt)
            .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
            .where(
                QuizSet.section_id == section.id,
                QuizAttempt.user_id == self.user_id,
            )
        ) or 0
        if assessed and not regeneration_feedback:
            raise AppError(
                "本节已有答题证据，不能由学习者重新生成；请联系管理员处理内容纠错",
                code="SECTION_ALREADY_ASSESSED",
                status=409,
            )
        if regeneration_feedback:
            if regeneration_feedback.get("contentVersionId") != existing.id:
                raise AppError(
                    "反馈对应的正文已经更新，请刷新后重新反馈",
                    code="FEEDBACK_CONTENT_VERSION_STALE",
                    status=409,
                )
            feedback_block = next(
                (
                    item
                    for item in load(existing.blocks_json, [])
                    if item.get("id") == regeneration_feedback.get("blockId")
                ),
                None,
            )
            if not feedback_block:
                raise AppError(
                    "反馈段落不属于当前正文版本",
                    code="FEEDBACK_BLOCK_STALE",
                    status=409,
                )
            feedback_hash = hashlib.sha256(
                json.dumps(
                    feedback_block,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if feedback_hash != regeneration_feedback.get("blockSnapshotHash"):
                raise AppError(
                    "反馈段落快照已变化，请刷新后重新反馈",
                    code="FEEDBACK_BLOCK_STALE",
                    status=409,
                )
    running = self.db.scalar(select(GenerationRun).where(GenerationRun.section_id == section.id, GenerationRun.status == "running").order_by(GenerationRun.started_at.desc()))
    if running:
        started = running.started_at if running.started_at.tzinfo else running.started_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - started).total_seconds() < 300:
            raise AppError("本节正在生成，请稍后读取状态", code="GENERATION_IN_PROGRESS", status=409)
        running.status, running.error_code, running.error_message, running.finished_at = "failed", "GENERATION_ABANDONED", "上一次生成超过 5 分钟未完成，已允许安全重试", now()
        self.db.commit()
    attempt = (self.db.scalar(select(func.max(GenerationRun.attempt)).where(GenerationRun.section_id == section.id)) or 0) + 1
    generation_operation = (
        "remediation"
        if retry
        else "regeneration"
        if regenerate
        else "lesson"
    )
    prior_failed_runs = self.db.scalars(
        select(GenerationRun)
        .where(
            GenerationRun.section_id == section.id,
            GenerationRun.operation == generation_operation,
            GenerationRun.status == "failed",
        )
        .order_by(GenerationRun.started_at.desc())
        .limit(10)
    ).all()
    carried_rejected_urls, carried_rejected_hosts = (
        source_blacklist_from_generation_traces(
            [load(item.trace_json, {}) for item in prior_failed_runs]
        )
    )
    regeneration_trace = {
        "regenerate": regenerate,
        "carriedSourceBlacklist": {
            "urlCount": len(carried_rejected_urls),
            "hostCount": len(carried_rejected_hosts),
        },
        **(
            {"supersedesRemediationId": superseded_remediation.id}
            if superseded_remediation
            else {}
        ),
        **(
            {
                "supersedesContentVersionId": existing.id,
                "supersedesQuizSetId": latest_quiz.id if latest_quiz else None,
            }
            if regenerate
            else {}
        ),
        **(
            {
                "feedbackId": regeneration_feedback.get("feedbackId"),
                "feedbackType": regeneration_feedback.get("feedbackType"),
                "feedbackContentVersionId": regeneration_feedback.get(
                    "contentVersionId"
                ),
                "feedbackBlockId": regeneration_feedback.get("blockId"),
                "feedbackBlockSnapshotHash": regeneration_feedback.get(
                    "blockSnapshotHash"
                ),
            }
            if regeneration_feedback
            else {}
        ),
    }
    run = GenerationRun(
        id=uid("generation"),
        section_id=section.id,
        operation=generation_operation,
        attempt=attempt,
        status="running",
        model=getattr(self.ai, "model", ""),
        trace_json=dump({
            "stage": "queued",
            "retry": retry,
            **regeneration_trace,
        }),
    )
    self.db.add(run)
    self.db.commit()
    stage_history: list[dict] = []
    active_stage_started = None

    def close_active_stage(finished_at):
        nonlocal active_stage_started
        if not stage_history or active_stage_started is None:
            return
        stage_history[-1]["finishedAt"] = timestamp(finished_at)
        stage_history[-1]["durationMs"] = max(
            0,
            int((finished_at - active_stage_started).total_seconds() * 1000),
        )
        active_stage_started = None

    def update_generation_stage(stage: str, **details):
        nonlocal active_stage_started
        started_at = now()
        close_active_stage(started_at)
        active_stage_started = started_at
        stage_history.append({
            "stage": stage,
            "startedAt": timestamp(started_at),
            **details,
        })
        run.trace_json = dump({
            "stage": stage,
            "retry": retry,
            **regeneration_trace,
            "stageHistory": stage_history,
            **details,
        })
        self.db.commit()
    try:
        prior = (
            immutable_questions_for_quiz(self.db, latest_quiz)
            if (retry or regenerate) and latest_quiz
            else []
        )
        remediation_targets: set[str] = set()
        remediation_diagnoses: list[dict] = []
        if retry and retry_attempt_id:
            failed_attempt = self.db.get(QuizAttempt, retry_attempt_id)
            failed_quiz = (
                self.db.get(QuizSet, failed_attempt.quiz_set_id)
                if failed_attempt
                else None
            )
            remediation_targets = failed_target_ids_for_attempt(
                self.db,
                attempt_id=retry_attempt_id,
            )
            if failed_quiz and remediation_targets:
                prior = [
                    question
                    for question in immutable_questions_for_quiz(
                        self.db,
                        failed_quiz,
                        require_versions=True,
                        require_evidence=True,
                    )
                    if question.get("assessmentTargetId") in remediation_targets
                ]
                remediation_diagnoses = diagnose_failed_attempt(
                    self.db, failed_attempt
                )
        book = self._book_for_section(section)
        remediation_strategy = (
            superseded_remediation.strategy
            if superseded_remediation
            else choose_remediation_strategy(remediation_diagnoses)
            if retry
            else None
        )
        memory = self._memory(book.shelf_id)
        memory_trace = {
            "memoryApplied": bool(memory),
            "memoryConceptCount": len(memory),
        }
        failed_attempt_context = (
            self.db.get(QuizAttempt, retry_attempt_id)
            if retry and retry_attempt_id
            else None
        )
        context_pack = self.generation_contexts.build(
            "remediation" if retry else "lesson_content",
            shelf=section_context.shelf,
            series=section_context.series,
            book=section_context.book,
            chapter=section_context.chapter,
            section=section,
            mission=mission_version,
            contract=contract,
            memory=memory,
            attempt=failed_attempt_context,
            feedback=regeneration_feedback,
        )
        regeneration_trace["knowledgeContext"] = (
            context_pack.knowledge_context.audit_manifest()
        )
        generation_mode = getattr(
            self.source_verifier,
            "generation_mode",
            "model_only",
        )
        if generation_mode not in {"model_only", "rights_grounded"}:
            generation_mode = "model_only"
        external_sources_allowed = (
            generation_mode == "rights_grounded"
            and bool(
                getattr(
                    self.source_verifier,
                    "allows_external_sources",
                    False,
                )
            )
        )
        rights_status = (
            "reviewed" if external_sources_allowed else "not_applicable"
        )
        factual_status = "unreviewed"
        lesson_request = self.generation_contexts.attach({
            **self._section_summary(section),
            "learningContractVersionId": contract.id,
            "missionVersionId": mission_version.id,
            "generationMode": generation_mode,
            "rightsStatus": rights_status,
            "factualStatus": factual_status,
            "assessmentTargets": assessment_contract_view(
                self.db, section, contract
            ),
            "rejectedSourceUrls": list(carried_rejected_urls),
            "rejectedSourceHosts": list(carried_rejected_hosts),
        }, context_pack)
        regeneration_trace["contextManifest"] = context_pack.manifest()
        if retry:
            lesson_request["remediationStrategy"] = remediation_strategy
            lesson_request["remediationDiagnosis"] = remediation_diagnoses
            if remediation_targets:
                lesson_request["assessmentTargets"] = [
                    item
                    for item in lesson_request["assessmentTargets"]
                    if item["assessmentTargetId"] in remediation_targets
                ]
                lesson_request["objectives"] = [
                    item["objective"]
                    for item in lesson_request["assessmentTargets"]
                ]
        lesson = None
        verification = []
        rejected_source_urls = list(carried_rejected_urls)
        rejected_source_hosts = list(carried_rejected_hosts)
        ai_harness_trace: list[dict] = []
        max_generation_attempts = 4
        if getattr(self.ai, "staged_lesson_generation", False):
            blueprint_builder = getattr(self.ai, "teaching_blueprint", None)
            if not retry and callable(blueprint_builder):
                update_generation_stage(
                    "teaching_blueprint",
                    **memory_trace,
                )
                blueprint = await blueprint_builder(lesson_request, memory)
                self._renew_generation_lease(resource_key, owner_id)
                blueprint_payload = blueprint.model_dump()
                lesson_request["teachingBlueprint"] = blueprint_payload
                regeneration_trace["teachingBlueprint"] = blueprint_payload
                regeneration_trace["generationVariant"] = (
                    "preference_aware_blueprint_v1"
                )
            update_generation_stage(
                "content_generation",
                sourceAttempt=1,
                maxSourceAttempts=max_generation_attempts,
                **memory_trace,
            )
            content_result = await self.ai.lesson_content(
                lesson_request,
                memory,
                prior,
            )
            assert_lesson_content_quality(content_result)
            self._renew_generation_lease(resource_key, owner_id)

            if not external_sources_allowed:
                content_result = model_only_content(content_result)
                verification = []
                update_generation_stage(
                    "model_only_content",
                    generationMode=generation_mode,
                    rightsStatus=rights_status,
                    factualStatus=factual_status,
                    **memory_trace,
                )

            for source_attempt in (
                range(1, max_generation_attempts + 1)
                if external_sources_allowed
                else ()
            ):
                update_generation_stage(
                    "source_verification",
                    sourceAttempt=source_attempt,
                    maxSourceAttempts=max_generation_attempts,
                    sourceUrls=[item.url for item in content_result.sources],
                    **memory_trace,
                )
                try:
                    verification = await self.source_verifier.verify(
                        content_result.sources
                    )
                    self._renew_generation_lease(resource_key, owner_id)
                    break
                except SourceVerificationError as error:
                    failed_sources = [
                        item.failure_dict() for item in error.failures
                    ]
                    rejected_source_urls.extend(
                        item["url"] for item in failed_sources
                    )
                    rejected_source_urls = list(
                        dict.fromkeys(rejected_source_urls)
                    )
                    rejected_source_hosts.extend(
                        host
                        for item in failed_sources
                        if item.get("reason") == "not_found"
                        and (host := urlparse(item["url"]).hostname)
                    )
                    rejected_source_hosts = list(
                        dict.fromkeys(rejected_source_hosts)
                    )
                    lesson_request["rejectedSourceUrls"] = (
                        rejected_source_urls
                    )
                    lesson_request["rejectedSourceHosts"] = (
                        rejected_source_hosts
                    )
                    if source_attempt == max_generation_attempts:
                        verification = degraded_source_verification_report(
                            content_result.sources,
                            error,
                        )
                        unverified_source_indexes = [
                            index
                            for index, item in enumerate(verification)
                            if item["verificationStatus"] == "failed"
                        ]
                        lesson_request["unverifiedSourceIndexes"] = (
                            unverified_source_indexes
                        )
                        lesson_request["contentReliability"] = (
                            "model_generated_unverified"
                        )
                        content_result = content_result.model_copy(
                            update={"confidence": "low"}
                        )
                        update_generation_stage(
                            "source_verification_degraded",
                            sourceAttempt=source_attempt,
                            maxSourceAttempts=max_generation_attempts,
                            failedSources=failed_sources,
                            unverifiedSourceIndexes=(
                                unverified_source_indexes
                            ),
                            rejectedSourceUrlCount=len(
                                rejected_source_urls
                            ),
                            rejectedSourceHostCount=len(
                                rejected_source_hosts
                            ),
                            **memory_trace,
                        )
                        self._renew_generation_lease(
                            resource_key,
                            owner_id,
                        )
                        break
                    update_generation_stage(
                        "source_repair",
                        sourceAttempt=source_attempt + 1,
                        maxSourceAttempts=max_generation_attempts,
                        failedSources=failed_sources,
                        rejectedSourceUrlCount=len(rejected_source_urls),
                        rejectedSourceHostCount=len(rejected_source_hosts),
                        **memory_trace,
                    )
                    previous_content = content_result
                    try:
                        source_repair_context = self.generation_contexts.build(
                            "source_repair",
                            shelf=section_context.shelf,
                            series=section_context.series,
                            book=section_context.book,
                            chapter=section_context.chapter,
                            section=section,
                            mission=mission_version,
                            contract=contract,
                            memory=memory,
                            attempt=failed_attempt_context,
                            interaction={
                                "failedSources": failed_sources,
                                "sourceAttempt": source_attempt,
                            },
                        )
                        source_repair_request = self.generation_contexts.attach(
                            {
                                key: value
                                for key, value in lesson_request.items()
                                if key != "generationContext"
                            },
                            source_repair_context,
                        )
                        content_result = (
                            await self.ai.repair_lesson_sources(
                                source_repair_request,
                                memory,
                                content_result,
                                failed_sources,
                                prior,
                            )
                        )
                        content_result = apply_source_repair_scope(
                            previous_content,
                            content_result,
                            failed_sources,
                        )
                    except AiError as repair_error:
                        if (
                            repair_error.code
                            != "SOURCE_REPAIR_SCOPE_VIOLATION"
                        ):
                            raise
                        content_result = previous_content
                        update_generation_stage(
                            "source_repair_rejected",
                            sourceAttempt=source_attempt + 1,
                            maxSourceAttempts=max_generation_attempts,
                            repairErrorCode=repair_error.code,
                            rejectedSourceUrlCount=len(
                                rejected_source_urls
                            ),
                            rejectedSourceHostCount=len(
                                rejected_source_hosts
                            ),
                            **memory_trace,
                        )
                        self._renew_generation_lease(
                            resource_key,
                            owner_id,
                        )
                        continue
                    assert_lesson_content_quality(content_result)
                    self._renew_generation_lease(resource_key, owner_id)

            quiz_result = None
            previous_quiz_rejection = None
            quiz_context_pack = self.generation_contexts.build(
                "lesson_quiz",
                shelf=section_context.shelf,
                series=section_context.series,
                book=section_context.book,
                chapter=section_context.chapter,
                section=section,
                mission=mission_version,
                contract=contract,
                memory=memory,
                attempt=failed_attempt_context,
            )
            quiz_request = self.generation_contexts.attach(
                {
                    key: value
                    for key, value in lesson_request.items()
                    if key != "generationContext"
                },
                quiz_context_pack,
            )
            for quiz_attempt in range(1, max_generation_attempts + 1):
                update_generation_stage(
                    "quiz_generation",
                    quizAttempt=quiz_attempt,
                    maxQuizAttempts=max_generation_attempts,
                    **(
                        {"previousRejection": previous_quiz_rejection}
                        if previous_quiz_rejection
                        else {}
                    ),
                    **memory_trace,
                )
                quiz_result = await self.ai.lesson_quiz(
                    quiz_request,
                    content_result,
                    prior,
                )
                self._renew_generation_lease(resource_key, owner_id)
                if prior and len(prior) == len(quiz_result.questions):
                    for question, previous in zip(
                        quiz_result.questions,
                        prior,
                        strict=True,
                    ):
                        question.objective = previous["objective"]
                        question.core = previous.get("core", False)
                        question.difficulty = "standard"
                if retry:
                    self._reorder_exact_remediation_duplicates(
                        prior,
                        quiz_result.questions,
                    )
                previous_quiz_rejection = self._questions_novelty_issue(
                    prior,
                    [item.model_dump() for item in quiz_result.questions],
                    allow_option_reorder_only=retry,
                ) if (retry or regenerate) else None
                if not previous_quiz_rejection:
                    break
                quiz_result = None
            if quiz_result is not None:
                lesson_schema = (
                    GeneratedRemediationLesson if retry else GeneratedLesson
                )
                lesson = lesson_schema(
                    **content_result.model_dump(),
                    questions=quiz_result.questions,
                )
            ai_harness_trace = self._ai_harness_trace()
        else:
            for novelty_attempt in range(1, max_generation_attempts + 1):
                lesson_request["rejectedSourceUrls"] = rejected_source_urls
                update_generation_stage(
                    "combined_generation",
                    noveltyAttempt=novelty_attempt,
                    maxGenerationAttempts=max_generation_attempts,
                    **memory_trace,
                )
                lesson = await self.ai.lesson(
                    lesson_request,
                    memory,
                    prior,
                )
                assert_lesson_content_quality(lesson)
                if retry:
                    self._reorder_exact_remediation_duplicates(
                        prior,
                        lesson.questions,
                    )
                self._renew_generation_lease(resource_key, owner_id)
                ai_harness_trace = self._ai_harness_trace()
                if not external_sources_allowed:
                    lesson = model_only_content(lesson)
                    verification = []
                    update_generation_stage(
                        "model_only_content",
                        noveltyAttempt=novelty_attempt,
                        maxGenerationAttempts=max_generation_attempts,
                        generationMode=generation_mode,
                        rightsStatus=rights_status,
                        factualStatus=factual_status,
                        **memory_trace,
                    )
                    if not (retry or regenerate) or self._questions_are_novel(
                        prior,
                        [item.model_dump() for item in lesson.questions],
                        allow_option_reorder_only=retry,
                    ):
                        break
                    lesson = None
                    continue
                update_generation_stage(
                    "source_verification",
                    noveltyAttempt=novelty_attempt,
                    maxGenerationAttempts=max_generation_attempts,
                    sourceUrls=[item.url for item in lesson.sources],
                    **memory_trace,
                )
                try:
                    verification = await self.source_verifier.verify(
                        lesson.sources
                    )
                    self._renew_generation_lease(resource_key, owner_id)
                except SourceVerificationError as error:
                    rejected_source_urls.extend(
                        item.url for item in error.failures
                    )
                    rejected_source_urls = list(
                        dict.fromkeys(rejected_source_urls)
                    )
                    if novelty_attempt < max_generation_attempts:
                        lesson = None
                        continue
                    verification = degraded_source_verification_report(
                        lesson.sources,
                        error,
                    )
                    unverified_source_indexes = [
                        index
                        for index, item in enumerate(verification)
                        if item["verificationStatus"] == "failed"
                    ]
                    lesson_request["unverifiedSourceIndexes"] = (
                        unverified_source_indexes
                    )
                    lesson_request["contentReliability"] = (
                        "model_generated_unverified"
                    )
                    lesson = lesson.model_copy(
                        update={"confidence": "low"}
                    )
                    update_generation_stage(
                        "source_verification_degraded",
                        noveltyAttempt=novelty_attempt,
                        maxGenerationAttempts=max_generation_attempts,
                        failedSources=[
                            item.failure_dict()
                            for item in error.failures
                        ],
                        unverifiedSourceIndexes=(
                            unverified_source_indexes
                        ),
                        **memory_trace,
                    )
                    self._renew_generation_lease(
                        resource_key,
                        owner_id,
                    )
                if not (retry or regenerate) or self._questions_are_novel(
                    prior,
                    [item.model_dump() for item in lesson.questions],
                    allow_option_reorder_only=retry,
                ):
                    break
                lesson = None
        if lesson is None:
            raise AppError("模型连续返回与旧题实质相同的题集", code="QUIZ_NOT_NOVEL", status=502)
        alignment_reviewer = getattr(
            self.ai,
            "review_lesson_alignment",
            None,
        )
        # Remediation intentionally teaches only the failed targets from the
        # frozen quiz attempt.  The legacy reviewer evaluates against the full
        # section contract, so applying it here rejects valid partial
        # remediation as if it were an incomplete replacement lesson.  The
        # remediation publisher below still enforces the deterministic target,
        # evidence-lineage, structure, and atomic-publication gates.
        if callable(alignment_reviewer) and not retry:
            update_generation_stage(
                "semantic_alignment_review",
                **memory_trace,
            )
            alignment = await alignment_reviewer(
                lesson_request,
                lesson,
                GeneratedQuiz(questions=lesson.questions),
            )
            self._renew_generation_lease(resource_key, owner_id)
            ai_harness_trace.extend(self._ai_harness_trace())
            regeneration_trace["semanticAlignment"] = alignment.model_dump()
            if not alignment.allowed:
                update_generation_stage(
                    "semantic_alignment_rejected",
                    semanticAlignment=alignment.model_dump(),
                    **memory_trace,
                )
                raise AppError(
                    "正文、学习目标与测验未形成语义闭环；内容未保存",
                    code="LESSON_SEMANTIC_ALIGNMENT_FAILED",
                    status=502,
                    retryable=True,
                )
        content = existing
        if not retry:
            content = ContentVersion(
                id=uid("content"),
                section_id=section.id,
                learning_contract_version_id=contract.id,
                version=(existing.version + 1 if existing else 1),
                blocks_json="[]",
                sources_json=dump([item.model_dump() for item in lesson.sources]),
                confidence=lesson.confidence,
                generation_mode=generation_mode,
                rights_status=rights_status,
                factual_status=factual_status,
                ai_generated=True,
                generation_run_id=run.id,
            )
            blocks = []
            for position, block in enumerate(lesson.blocks, 1):
                payload = block.model_dump()
                payload["id"] = f"block_{content.id}_{position}"
                payload["version"] = content.version
                blocks.append(payload)
            content.blocks_json = dump(blocks)
            attach_content_compliance_metadata(content, run)
        question_payloads = bind_questions_to_targets(
            self.db,
            section,
            [item.model_dump() for item in lesson.questions],
            contract,
        )
        quiz_generation = latest_quiz.generation + 1 if latest_quiz else 1
        quiz = None
        if not retry:
            quiz = QuizSet(
                id=uid("quiz"),
                section_id=section.id,
                content_version_id=content.id,
                learning_contract_version_id=contract.id,
                generation=quiz_generation,
                questions_json=dump(question_payloads),
                schema_version=VERSIONED_LEGACY_QUIZ_SCHEMA,
            )
        claim_reports = []
        if not retry and external_sources_allowed:
            verify_claims = getattr(
                self.source_verifier,
                "verify_claims",
                None,
            )
            if callable(verify_claims):
                candidates = generated_claim_verification_candidates(
                    content,
                    quiz,
                )
                update_generation_stage(
                    "semantic_claim_verification",
                    claimCandidateCount=len(candidates),
                    **memory_trace,
                )
                claim_reports = await verify_claims(candidates)
                self._renew_generation_lease(resource_key, owner_id)

        if regenerate and not regeneration_feedback:
            assessed_after_generation = self.db.scalar(
                select(func.count())
                .select_from(QuizAttempt)
                .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
                .where(
                    QuizSet.section_id == section.id,
                    QuizAttempt.user_id == self.user_id,
                )
            ) or 0
            if assessed_after_generation:
                raise AppError(
                    "生成期间已产生答题证据，新版本未保存",
                    code="SECTION_ALREADY_ASSESSED",
                    status=409,
                )

        # No external I/O is allowed after this point. SQLite has one
        # database-wide writer; keep publication as one short transaction
        # so the same boundary remains valid after a PostgreSQL migration.
        update_generation_stage("persistence", **memory_trace)
        if not retry:
            self.db.add(content)
            self.db.flush()
            self.db.add(
                SourceVerification(
                    id=uid("verification"),
                    content_version_id=content.id,
                    report_json=dump(verification),
                )
            )
        if not retry:
            self.db.add(quiz)
            self.db.flush()
            governance = persist_generated_governance(
                self.db,
                content=content,
                quiz=quiz,
                source_verification=verification,
                actor_id=run.id,
            )
            if claim_reports:
                for report in claim_reports:
                    record_verified_claim_binding(
                        self.db,
                        source_claim_version_id=report[
                            "sourceClaimVersionId"
                        ],
                        source_version_id=report["sourceVersionId"],
                        locator_type=report["locatorType"],
                        locator=report["locator"],
                        excerpt_text=report["excerptText"],
                        support_type=report["supportType"],
                        verification_mode=report["verificationMode"],
                        verification_rule_version=report[
                            "verificationRuleVersion"
                        ],
                        report=report.get("report", {}),
                        actor_id=run.id,
                    )
                governance = reevaluate_generated_governance(
                    self.db,
                    quiz_id=quiz.id,
                    actor_id=run.id,
                )
            regeneration_trace["governanceDecision"] = governance
            if regenerate:
                self._rebind_regenerated_section(
                    learning_run=learning_run,
                    section=section,
                    superseded_content=existing,
                    content=content,
                    quiz=quiz,
                    generation_run=run,
                    regeneration_feedback=regeneration_feedback,
                )
            block_id_by_position = {
                item.position: item.id
                for item in self.db.scalars(
                    select(ContentBlockVersion)
                    .where(ContentBlockVersion.content_version_id == content.id)
                    .order_by(ContentBlockVersion.position)
                ).all()
            }
            evidence_block_ids_by_position = [
                [
                    block_id_by_position[index]
                    for index in question.get("claim_block_indexes", [])
                    if (
                        isinstance(index, int)
                        and not isinstance(index, bool)
                        and index in block_id_by_position
                    )
                ]
                for question in question_payloads
            ]
            question_payloads = publish_assessment_item_versions(
                self.db,
                quiz=quiz,
                questions=question_payloads,
                evidence_block_ids_by_position=evidence_block_ids_by_position,
                uid=uid,
            )
        else:
            if not retry_attempt_id or not failed_attempt or not failed_quiz:
                raise AppError(
                    "补救教学必须绑定失败答题",
                    code="REMEDIATION_ATTEMPT_REQUIRED",
                )
            remediation_blocks = [
                block.model_dump() for block in lesson.blocks
            ]
            published_remediation = publish_remediation_candidate(
                self.db,
                uid=uid,
                section=section,
                contract=contract,
                source_content=content,
                source_quiz=failed_quiz,
                source_attempt=failed_attempt,
                generation_run=run,
                quiz_generation=quiz_generation,
                questions=question_payloads,
                prior_questions=prior,
                remediation_blocks=remediation_blocks,
                failed_target_ids=remediation_targets,
                strategy=remediation_strategy,
                diagnosis_snapshot=remediation_diagnoses,
                superseded_remediation=superseded_remediation,
            )
            quiz = published_remediation.quiz
            governance = published_remediation.governance
            regeneration_trace["governanceDecision"] = governance
        finished_at = now()
        close_active_stage(finished_at)
        run.status, run.finished_at = "succeeded", finished_at
        run.trace_json = dump({
            "stage": "persisted",
            "contentVersionId": content.id if content else None,
            "quizSetId": quiz.id,
            "sourceVerification": verification,
            "aiHarness": ai_harness_trace,
            "stageHistory": stage_history,
            **regeneration_trace,
            **memory_trace,
        })
        self.db.commit()
        if not isinstance(self.scope, WorkerExecutionContext) and not retry:
            open_run_section(
                self.db,
                run=learning_run,
                section=section,
                mission_version_id=mission_version.id,
                source="interactive_generate",
                uid=uid,
                preferred_quiz_id=quiz.id,
            )
            self.db.commit()
        return self.section(section.id)
    except BaseException as error:
        failure_finished_at = now()
        close_active_stage(failure_finished_at)
        self.db.rollback()
        operation_id = run.id
        run = self.db.get(GenerationRun, operation_id)
        if run:
            run.status = "failed"
            run.error_code = getattr(error, "code", type(error).__name__)
            run.error_message = (
                str(error)[:2000]
                if isinstance(error, AppError)
                else "生成失败，请稍后重试"
            )
            run.finished_at = failure_finished_at
            previous_trace = load(run.trace_json, {})
            harness_trace = self._ai_harness_trace()
            run.trace_json = dump({
                **previous_trace,
                "stage": "failed",
                "stageHistory": stage_history,
                **({"aiHarness": harness_trace} if harness_trace else {}),
            })
            self.db.commit()
        if isinstance(error, AppError):
            if error.operation_id is None:
                error.operation_id = operation_id
            raise
        if isinstance(error, (CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        raise AiError(
            "小节生成失败；失败状态已保存，可安全重试",
            operation_id=operation_id,
        ) from error
