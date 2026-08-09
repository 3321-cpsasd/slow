"""Section generation orchestration extracted from the application facade.

This module owns the v2 one-call coordinator and lazily delegates the isolated
legacy remediation/test pipeline. SlowService supplies only shared application
collaborators and read-model callbacks.
"""

from asyncio import CancelledError
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, select

from ..auth.context import WorkerExecutionContext
from ..core.errors import AiError, AppError, safe_error_code
from ..infrastructure.tables import (
    ContentVersion,
    GenerationRun,
    LearningContractVersion,
    LearningMissionVersion,
    LearningResumePosition,
    LearningRun,
    LearningRunSectionBinding,
    QuizAttempt,
    QuizSet,
    Section,
    now,
)
from ..modules.learning.assessment import assessment_contract_view
from ..modules.learning.contracts import ensure_learning_contract, open_run_section
from ..modules.learning.generation_leases import renew_generation_lease
from ..services.source_verifier import SourceVerificationError
from .lesson_generation import (
    CandidateValidationFailure,
    LessonGenerationSpec,
    NeighborBoundary,
    LessonTargetSpec,
    LESSON_CONTEXT_POLICY_VERSION,
    LESSON_GENERATION_PIPELINE_VERSION,
    LESSON_GENERATION_PROMPT_VERSION,
    LESSON_GENERATION_RULE_VERSION,
    LESSON_GENERATION_SCHEMA_VERSION,
    publish_lesson_candidate,
    validate_lesson_candidate,
)


CONTENT_COMPLIANCE_RULE_VERSION = "content_compliance_v1"
MODEL_ONLY_PROMPT_VERSION = "lesson_content_model_only_v1"
AI_CONTENT_LABEL_SCHEMA_VERSION = "ai_content_label_v1"
GENERATION_ARTIFACT_MARKERS = (
    "候选 JSON",
    "原始候选",
    "目标 JSON Schema",
    "JSON 结构修复器",
    "无法恢复原有事实内容",
    "最小可验证结构",
    "服务端校验拒绝",
)


def uid(prefix):
    return f"{prefix}_{uuid4().hex}"


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def load(value, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def timestamp(value):
    return value.isoformat() if value else None


def model_only_content(value):
    return value.model_copy(
        update={
            "sources": [],
            "blocks": [
                block.model_copy(update={"source_indexes": []})
                for block in value.blocks
            ],
        }
    )


def content_output_hash(blocks_json: str, sources_json: str) -> str:
    payload = {
        "blocks": load(blocks_json, []),
        "sources": load(sources_json, []),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attach_content_compliance_metadata(
    content: ContentVersion,
    run: GenerationRun,
    *,
    prompt_version: str = MODEL_ONLY_PROMPT_VERSION,
) -> None:
    content.output_hash = content_output_hash(
        content.blocks_json,
        content.sources_json,
    )
    content.labeling_metadata_json = dump(
        {
            "schemaVersion": AI_CONTENT_LABEL_SCHEMA_VERSION,
            "generatedContent": content.ai_generated,
            "serviceProvider": "Slow",
            "contentId": content.id,
            "generationRunId": run.id,
            "generationMode": content.generation_mode,
            "rightsStatus": content.rights_status,
            "factualStatus": content.factual_status,
            "model": run.model,
            "promptVersion": prompt_version,
            "ruleVersion": CONTENT_COMPLIANCE_RULE_VERSION,
            "outputHash": content.output_hash,
        }
    )


def normalized(value: str):
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def assert_lesson_content_quality(lesson) -> None:
    combined = "\n".join(
        f"{block.heading}\n{block.content}"
        for block in lesson.blocks
    )
    marker = next(
        (value for value in GENERATION_ARTIFACT_MARKERS if value in combined),
        None,
    )
    if marker:
        raise AiError(
            "AI 返回了生成过程说明而不是教材正文；内容未保存，可安全重新生成",
            code="AI_CONTENT_QUALITY_FAILED",
        )


def apply_source_repair_scope(before, candidate, failed_sources: list[dict]):
    failed_urls = {item["url"] for item in failed_sources}
    failed_indexes = {
        index
        for index, source in enumerate(before.sources)
        if source.url in failed_urls
    }
    if len(before.sources) != len(candidate.sources) or len(before.blocks) != len(candidate.blocks):
        raise AiError(
            "来源修复改变了教材整体结构；内容未保存",
            code="SOURCE_REPAIR_SCOPE_VIOLATION",
        )
    if not failed_indexes:
        raise AiError(
            "来源修复没有匹配到失败来源；内容未保存",
            code="SOURCE_REPAIR_SCOPE_VIOLATION",
        )
    merged = before.model_copy(deep=True)
    for index, new_source in enumerate(candidate.sources):
        if index not in failed_indexes and new_source != before.sources[index]:
            raise AiError(
                "来源修复改变了无需替换的来源顺序或内容",
                code="SOURCE_REPAIR_SCOPE_VIOLATION",
            )
        if index in failed_indexes and new_source.url in failed_urls:
            raise AiError(
                "来源修复仍返回服务端已拒绝的来源",
                code="SOURCE_REPAIR_SCOPE_VIOLATION",
            )
        if index in failed_indexes:
            merged.sources[index] = new_source
    for index, (old_block, new_block) in enumerate(
        zip(before.blocks, candidate.blocks, strict=True)
    ):
        if set(old_block.source_indexes) & failed_indexes:
            merged.blocks[index] = new_block
    return merged


def source_blacklist_from_generation_traces(
    traces: list[dict],
) -> tuple[list[str], list[str]]:
    rejected_urls: list[str] = []
    rejected_hosts: list[str] = []
    for trace in traces:
        for stage in trace.get("stageHistory", []):
            if stage.get("stage") not in {
                "source_repair",
                "source_verification_degraded",
                "source_verification_failed",
            }:
                continue
            for failure in stage.get("failedSources", []):
                if failure.get("reason") != "not_found":
                    continue
                url = failure.get("url")
                if not isinstance(url, str) or not url:
                    continue
                if url not in rejected_urls:
                    rejected_urls.append(url)
                host = urlparse(url).hostname
                if host and host not in rejected_hosts:
                    rejected_hosts.append(host)
    return rejected_urls, rejected_hosts


def degraded_source_verification_report(
    sources,
    error: SourceVerificationError,
) -> list[dict]:
    available: dict[str, list] = {}
    for result in error.results:
        available.setdefault(result.url, []).append(result)
    report = []
    for source in sources:
        matches = available.get(source.url, [])
        if matches:
            report.append(matches.pop(0).as_dict())
            continue
        report.append(
            {
                "url": source.url,
                "reachable": False,
                "statusCode": 0,
                "pinned": source.kind != "source_code" or source.version in source.url,
                "verificationStatus": "failed",
            }
        )
    return report


class SectionGenerationCoordinator:
    def __init__(self, host):
        self.db = host.db
        self.ai = host.ai
        self.source_verifier = host.source_verifier
        self.scope = host.scope
        self.user_id = host.user_id
        self.contexts = host.contexts
        self.progress = host.progress
        self.missions = host.missions
        self.generation_contexts = host.generation_contexts
        self._section_summary = host._section_summary
        self._book_for_section = host._book_for_section
        self._memory = host._memory
        self.section = host.section

    def _renew_generation_lease(self, resource_key, owner_id):
        if resource_key and owner_id and not renew_generation_lease(
            self.db,
            resource_key,
            owner_id,
        ):
            raise AppError(
                "生成租约已失效，已停止旧请求写入",
                code="GENERATION_LEASE_LOST",
                status=409,
            )

    async def generate(
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
                    "当前 AI 适配器不支持 lesson_generation_v2；拒绝回退旧链路",
                    code="LESSON_GENERATION_V2_UNSUPPORTED",
                    status=500,
                )
        return await self._generate_legacy_section(
            section_id,
            retry=retry,
            retry_attempt_id=retry_attempt_id,
            regenerate=regenerate,
            supersede_remediation_id=supersede_remediation_id,
            regeneration_feedback=regeneration_feedback,
            resource_key=resource_key,
            owner_id=owner_id,
        )

    async def _generate_section_v2(
        self,
        section_id: str,
        *,
        regenerate: bool,
        regeneration_feedback: dict | None,
        resource_key: str | None,
        owner_id: str | None,
    ):
        """One-call lesson generation followed by a deterministic atomic publish."""

        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = section_context.section
        learning_run = self.progress.active_run(section_context.series.id)
        mission_version = self.missions.current_version(section_context.series.id)
        contract = None
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
        contract = contract or ensure_learning_contract(
            self.db,
            section,
            mission_version_id=mission_version.id,
            provenance_mode="native_m2",
        )
        section_progress = self.progress.for_section(
            section,
            section_context.chapter,
            section_context.book,
        )
        if section_progress.status == "locked":
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
                ContentVersion.publication_status == "published",
            )
            .order_by(ContentVersion.version.desc())
        )
        latest_quiz = (
            self.db.scalar(
                select(QuizSet)
                .where(
                    QuizSet.section_id == section.id,
                    QuizSet.content_version_id == existing.id,
                    QuizSet.learning_contract_version_id == contract.id,
                    QuizSet.publication_status == "published",
                )
                .order_by(QuizSet.generation.desc())
            )
            if existing
            else None
        )
        if existing and not regenerate:
            return self.section(section.id)
        if regenerate:
            if not existing:
                raise AppError(
                    "本节还没有已发布正文，请直接生成",
                    code="SECTION_CONTENT_MISSING",
                    status=409,
                )
            if not latest_quiz:
                raise AppError(
                    "当前正文缺少匹配的已发布验证题，不能安全重新生成",
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
                    "本节已有答题证据，不能由学习者重新生成；请提交内容纠错",
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

        running = self.db.scalar(
            select(GenerationRun)
            .where(
                GenerationRun.section_id == section.id,
                GenerationRun.status.in_(("running", "generating", "validating")),
            )
            .order_by(GenerationRun.started_at.desc())
        )
        if running:
            started = (
                running.started_at
                if running.started_at.tzinfo
                else running.started_at.replace(tzinfo=timezone.utc)
            )
            if (datetime.now(timezone.utc) - started).total_seconds() < 300:
                raise AppError(
                    "本节正在生成，请稍后读取状态",
                    code="GENERATION_IN_PROGRESS",
                    status=409,
                )
            running.status = "failed"
            running.error_code = "GENERATION_ABANDONED"
            running.error_message = "上一次生成超过 5 分钟未完成，已允许安全重试"
            running.finished_at = now()
            self.db.commit()

        attempt_number = (
            self.db.scalar(
                select(func.max(GenerationRun.attempt)).where(
                    GenerationRun.section_id == section.id
                )
            )
            or 0
        ) + 1
        memory = self._memory(section_context.shelf.id)
        context_pack = self.generation_contexts.build(
            "lesson_content",
            shelf=section_context.shelf,
            series=section_context.series,
            book=section_context.book,
            chapter=section_context.chapter,
            section=section,
            mission=mission_version,
            contract=contract,
            memory=memory,
            feedback=regeneration_feedback,
        )
        context_payload = context_pack.payload()
        target_payloads = assessment_contract_view(self.db, section, contract)
        target_ids = {item["assessmentTargetId"] for item in target_payloads}
        chapter_sections = self.db.scalars(
            select(Section)
            .where(Section.chapter_id == section.chapter_id)
            .order_by(Section.position)
        ).all()
        current_index = next(
            index for index, item in enumerate(chapter_sections) if item.id == section.id
        )
        neighbors: list[NeighborBoundary] = []
        for direction, neighbor_index in (
            ("previous", current_index - 1),
            ("next", current_index + 1),
        ):
            if not 0 <= neighbor_index < len(chapter_sections):
                continue
            neighbor = chapter_sections[neighbor_index]
            neighbor_objectives = load(neighbor.objectives_json, []) or []
            neighbors.append(
                NeighborBoundary(
                    direction=direction,
                    sectionId=neighbor.id,
                    title=neighbor.title,
                    question=neighbor.question,
                    objectives=[
                        str(
                            item.get("statement") or item.get("objective") or ""
                            if isinstance(item, dict)
                            else item
                        )
                        for item in neighbor_objectives
                        if str(
                            item.get("statement") or item.get("objective") or ""
                            if isinstance(item, dict)
                            else item
                        ).strip()
                    ],
                )
            )
        learner = context_payload["learner"]
        spec = LessonGenerationSpec(
            generationMode=(
                "demo" if getattr(self.ai, "configured", True) is False else "model_only"
            ),
            mission=context_payload.get("mission") or {},
            learner={
                key: learner.get(key)
                for key in (
                    "profession",
                    "stage",
                    "purpose",
                    "experience",
                    "planRole",
                    "planExperience",
                    "preferences",
                )
            },
            section={
                "id": section.id,
                "title": section.title,
                "question": section.question,
                "bookTitle": section_context.book.title,
                "chapterTitle": section_context.chapter.title,
                "chapterObjective": section_context.chapter.objective,
            },
            learningContractVersionId=contract.id,
            learningContractVersion=contract.version,
            targets=[
                LessonTargetSpec.model_validate(
                    {
                        key: item[key]
                        for key in (
                            "assessmentTargetId",
                            "conceptRevisionId",
                            "objective",
                            "dimension",
                            "targetDepth",
                            "required",
                            "verificationPolicy",
                        )
                    }
                )
                for item in target_payloads
            ],
            neighborBoundaries=neighbors,
            relevantMastery=[
                item
                for item in memory
                if item.get("assessmentTargetId") in target_ids
            ],
            knowledgeContext=context_payload["knowledgeContext"],
            depthPolicy=context_payload["policy"]["depthPolicy"],
            feedback=regeneration_feedback or {},
            rightsAssetVersionIds=[],
        )
        run = GenerationRun(
            id=uid("generation"),
            section_id=section.id,
            operation="regeneration" if regenerate else "lesson",
            attempt=attempt_number,
            status="generating",
            model=getattr(self.ai, "model", ""),
            pipeline_version=LESSON_GENERATION_PIPELINE_VERSION,
            prompt_version=LESSON_GENERATION_PROMPT_VERSION,
            schema_version=LESSON_GENERATION_SCHEMA_VERSION,
            generation_mode=spec.generation_mode,
            context_hash=spec.context_hash(),
            trace_json=dump(
                {
                    "stage": "generating",
                    "pipelineVersion": LESSON_GENERATION_PIPELINE_VERSION,
                    "promptVersion": LESSON_GENERATION_PROMPT_VERSION,
                    "schemaVersion": LESSON_GENERATION_SCHEMA_VERSION,
                    "contextPolicyVersion": LESSON_CONTEXT_POLICY_VERSION,
                    "contextHash": spec.context_hash(),
                    "contractVersionId": contract.id,
                    "knowledgeContext": (
                        context_pack.knowledge_context.audit_manifest()
                    ),
                    "generationMode": spec.generation_mode,
                    "physicalCallBudget": 1,
                    "regenerate": regenerate,
                    **(
                        {"feedbackId": regeneration_feedback.get("feedbackId")}
                        if regeneration_feedback
                        else {}
                    ),
                }
            ),
        )
        self.db.add(run)
        self.db.commit()
        try:
            candidate = await self.ai.generate_lesson(spec.payload())
            self._renew_generation_lease(resource_key, owner_id)
            run.status = "validating"
            run.trace_json = dump(
                {
                    **load(run.trace_json, {}),
                    "stage": "validating",
                    "candidateBlockCount": len(candidate.blocks),
                    "candidateQuestionCount": len(candidate.questions),
                    "aiHarness": self._ai_harness_trace(),
                }
            )
            self.db.commit()
            try:
                validated = validate_lesson_candidate(spec, candidate)
            except CandidateValidationFailure as failure:
                raise AppError(
                    failure.message,
                    code=failure.code,
                    status=409
                    if failure.code == "PREREQUISITE_GAP_REQUIRES_REPLAN"
                    else 502,
                    retryable=False,
                    details=failure.location,
                ) from failure

            if regenerate:
                assessed_after_generation = self.db.scalar(
                    select(func.count())
                    .select_from(QuizAttempt)
                    .join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id)
                    .where(
                        QuizSet.section_id == section.id,
                        QuizAttempt.user_id == self.user_id,
                    )
                ) or 0
                if assessed_after_generation and not regeneration_feedback:
                    raise AppError(
                        "生成期间已产生答题证据，新版本未保存",
                        code="SECTION_ALREADY_ASSESSED",
                        status=409,
                    )

            next_content_version = (
                self.db.scalar(
                    select(func.max(ContentVersion.version)).where(
                        ContentVersion.section_id == section.id
                    )
                )
                or 0
            ) + 1
            next_quiz_generation = (
                self.db.scalar(
                    select(func.max(QuizSet.generation)).where(
                        QuizSet.section_id == section.id
                    )
                )
                or 0
            ) + 1
            published = publish_lesson_candidate(
                self.db,
                uid=uid,
                section=section,
                contract=contract,
                generation_run=run,
                spec=spec,
                validated=validated,
                content_version=next_content_version,
                quiz_generation=next_quiz_generation,
                superseded_content=existing if regenerate else None,
                superseded_quiz=latest_quiz if regenerate else None,
            )
            if regenerate:
                self._rebind_regenerated_section(
                    learning_run=learning_run,
                    section=section,
                    superseded_content=existing,
                    content=published.content,
                    quiz=published.quiz,
                    generation_run=run,
                    regeneration_feedback=regeneration_feedback,
                )
            if not isinstance(self.scope, WorkerExecutionContext):
                open_run_section(
                    self.db,
                    run=learning_run,
                    section=section,
                    mission_version_id=mission_version.id,
                    source="interactive_generate",
                    uid=uid,
                    preferred_quiz_id=published.quiz.id,
                )
            finished_at = now()
            run.status = "succeeded"
            run.finished_at = finished_at
            run.trace_json = dump(
                {
                    **load(run.trace_json, {}),
                    "stage": "published",
                    "ruleVersion": LESSON_GENERATION_RULE_VERSION,
                    "contentVersionId": published.content.id,
                    "quizSetId": published.quiz.id,
                    "publicationStatus": "published",
                    "contentKnowledgeClaimVersionIds": sorted(
                        {
                            claim_id
                            for block in candidate.blocks
                            for claim_id in block.claim_version_ids
                        }
                    ),
                    **(
                        {
                            "feedbackReplacement": {
                                "sourceBlockId": (
                                    candidate.feedback_replacement.source_block_id
                                ),
                                "replacementBlockKey": (
                                    candidate.feedback_replacement.replacement_block_key
                                ),
                                "replacementBlockId": (
                                    published.feedback_replacement_block_id
                                ),
                            }
                        }
                        if candidate.feedback_replacement
                        else {}
                    ),
                }
            )
            self.db.commit()
        except BaseException as error:
            failure_finished_at = now()
            self.db.rollback()
            operation_id = run.id
            failed_run = self.db.get(GenerationRun, operation_id)
            if failed_run:
                failed_run.status = "failed"
                failed_run.error_code = safe_error_code(error)
                failed_run.error_message = (
                    str(error)[:2000]
                    if isinstance(error, AppError)
                    else "生成失败，请稍后重试"
                )
                failed_run.finished_at = failure_finished_at
                failed_run.trace_json = dump(
                    {
                        **load(failed_run.trace_json, {}),
                        "stage": "failed",
                        **(
                            {"validationDetails": error.details}
                            if isinstance(error, AppError) and error.details
                            else {}
                        ),
                    }
                )
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
        # Publication is already committed and authoritative at this point.
        # Read-model failures must not rewrite the successful generation audit.
        return self.section(section.id)

    async def _generate_legacy_section(
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
        from .legacy_section_generation import generate_legacy_section

        return await generate_legacy_section(
            self,
            section_id,
            retry=retry,
            retry_attempt_id=retry_attempt_id,
            regenerate=regenerate,
            supersede_remediation_id=supersede_remediation_id,
            regeneration_feedback=regeneration_feedback,
            resource_key=resource_key,
            owner_id=owner_id,
        )

    def _rebind_regenerated_section(
        self,
        *,
        learning_run: LearningRun,
        section: Section,
        superseded_content: ContentVersion,
        content: ContentVersion,
        quiz: QuizSet,
        generation_run: GenerationRun,
        regeneration_feedback: dict | None,
    ) -> None:
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section.id,
            )
        )
        if not binding:
            return
        if binding.content_version_id != superseded_content.id:
            raise AppError(
                "当前学习实例的正文版本已经变化，新版本未绑定",
                code="SECTION_BINDING_STALE",
                status=409,
            )
        previous_content_id = binding.content_version_id
        previous_quiz_id = binding.initial_quiz_set_id
        audit = load(binding.lineage_audit_json, {})
        regeneration_history = list(audit.get("regenerations") or [])
        regeneration_history.append({
            "generationRunId": generation_run.id,
            "feedbackId": (
                regeneration_feedback.get("feedbackId")
                if regeneration_feedback
                else None
            ),
            "fromContentVersionId": previous_content_id,
            "fromQuizSetId": previous_quiz_id,
            "toContentVersionId": content.id,
            "toQuizSetId": quiz.id,
            "changedAt": timestamp(now()),
        })
        binding.learning_contract_version_id = content.learning_contract_version_id
        binding.content_version_id = content.id
        binding.initial_quiz_set_id = quiz.id
        binding.source = (
            "feedback_regeneration"
            if regeneration_feedback
            else "interactive_regeneration"
        )
        binding.source_fact_id = (
            regeneration_feedback.get("feedbackId")
            if regeneration_feedback
            else generation_run.id
        )
        binding.lineage_audit_json = dump({
            **audit,
            "missionVersionId": (
                audit.get("missionVersionId")
                or learning_run.initial_mission_version_id
            ),
            "contractVersionId": content.learning_contract_version_id,
            "contentVersionId": content.id,
            "quizSetId": quiz.id,
            "regenerations": regeneration_history,
        })
        resume = self.db.scalar(
            select(LearningResumePosition).where(
                LearningResumePosition.user_id == self.user_id,
                LearningResumePosition.learning_run_id == learning_run.id,
                LearningResumePosition.section_id == section.id,
            )
        )
        if resume:
            resume.learning_contract_version_id = content.learning_contract_version_id
            resume.content_version_id = content.id
            resume.block_id = ""
            resume.updated_at = now()

    def _ai_harness_trace(self) -> list[dict]:
        trace = getattr(self.ai, "structured_trace", None)
        if not callable(trace):
            return []
        value = trace()
        return value if isinstance(value, list) else []

    def _questions_are_novel(self, prior, current):
        return self._questions_novelty_issue(prior, current) is None

    def _questions_novelty_issue(self, prior, current):
        if not prior:
            return "prior_questions_missing"
        if len(prior) != len(current):
            return "question_count_mismatch"
        if Counter(item["objective"] for item in prior) != Counter(item["objective"] for item in current):
            return "objective_set_mismatch"
        prior_by_objective = {}
        for item in prior:
            prior_by_objective.setdefault(item["objective"], []).append(item)
        for question in current:
            candidates = prior_by_objective.get(question["objective"], [])
            if any(
                normalized(question["prompt"]) == normalized(old["prompt"])
                for old in candidates
            ):
                return "prompt_duplicate"
            if any(
                {normalized(option) for option in question["options"]}
                == {normalized(option) for option in old["options"]}
                for old in candidates
            ):
                return "options_duplicate"
            if question.get("difficulty", "standard") != "standard":
                return "difficulty_mismatch"
        return None
