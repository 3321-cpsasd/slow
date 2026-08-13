from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...application.generation_context import GenerationContextBuilder
from ...core.errors import AiError, AppError, safe_error_code
from ...infrastructure.tables import (
    AssessmentTarget,
    AskMeDiscussionCommand,
    AskMeDiscussionSession,
    AskMeDiscussionTopic,
    AskMeDiscussionTurnRecord,
    AskMeSession,
    ContentBlockAssessmentTarget,
    ContentBlockVersion,
    ContentVersion,
    GenerationRun,
    LearningContractAssessmentTarget,
    LearningContractVersion,
    LearningMissionVersion,
    LearningRunSectionBinding,
    now,
)
from ..learning.contracts import open_run_section
from ..learning.progress import ProgressStore


class AskMeService:
    """Runs legacy three-stage checks and user-controlled topic discussions."""

    DIMENSIONS = ("mechanism", "boundary", "transfer")
    TARGET_BLOCK_ROLES = {
        "mechanism": ("mechanism", "core_instruction"),
        "boundary": ("boundary", "comparison"),
        "transfer": ("transfer", "application", "practice"),
    }
    DISCUSSION_TURN_LEASE = timedelta(minutes=10)

    def __init__(
        self,
        db: Session,
        *,
        user_id: str,
        tutor,
        contexts,
        progress: ProgressStore,
        missions,
        generation_contexts: GenerationContextBuilder,
        section_reader: Callable,
        memory_loader: Callable,
        evidence_recorder: Callable,
        evidence_context: Callable,
        uid: Callable[[str], str],
        dump: Callable,
        load: Callable,
    ):
        self.db = db
        self.user_id = user_id
        self.tutor = tutor
        self.contexts = contexts
        self.progress = progress
        self.missions = missions
        self.generation_contexts = generation_contexts
        self.section_reader = section_reader
        self.memory_loader = memory_loader
        self.evidence_recorder = evidence_recorder
        self.evidence_context = evidence_context
        self.uid = uid
        self.dump = dump
        self.load = load

    def _author_lineage(self, binding) -> dict[str, str]:
        content = self.db.get(ContentVersion, binding.content_version_id)
        generation = (
            self.db.get(GenerationRun, content.generation_run_id)
            if content and content.generation_run_id
            else None
        )
        if not generation or not generation.model:
            return {}
        attempts = self.load(generation.trace_json, {}).get("modelAttempts", [])
        selected = next(
            (
                item
                for item in reversed(attempts)
                if item.get("outcome") == "succeeded"
            ),
            {},
        )
        return {
            "authorModel": generation.model,
            "authorDeploymentId": str(selected.get("deploymentId") or ""),
            "authorModelFamilyId": str(selected.get("modelFamilyId") or ""),
        }

    async def answer(self, section_id: str, answer: str | None):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        section = context.section
        learning_run = self.progress.active_run(context.series.id)
        if not self.progress.for_section(
            section,
            context.chapter,
            context.book,
        ).ask_me_unlocked:
            raise AppError(
                "小节满分后才解锁深入讨论",
                code="ASK_ME_LOCKED",
                status=403,
            )
        binding = self._binding(learning_run.id, section.id)
        if not binding:
            mission = self.missions.current_version(context.series.id)
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=section,
                mission_version_id=mission.id,
                source="ask_me_start_recovery",
                uid=self.uid,
            )
            self.db.commit()
        session = self._session(learning_run.id, section.id)
        entries = self.load(session.entries_json, []) if session else []
        if session and session.status == "completed":
            return self.view(session)
        if not session:
            return await self._start(context, learning_run, binding, answer)
        if not answer:
            raise AppError("本轮回答不能为空", code="ASK_ME_ANSWER_REQUIRED")
        return await self._continue(context, binding, session, entries, answer)

    def discussion(self, section_id: str):
        context, learning_run = self._discussion_context(section_id)
        session = self._discussion_session(learning_run.id, section_id)
        return self.discussion_view(session) if session else None

    def start_discussion(self, section_id: str):
        context, learning_run = self._discussion_context(section_id)
        existing = self._discussion_session(learning_run.id, section_id)
        if existing:
            return self.discussion_view(existing)
        binding = self._binding(learning_run.id, section_id)
        if not binding:
            mission = self.missions.current_version(context.series.id)
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=context.section,
                mission_version_id=mission.id,
                source="ask_me_discussion_start",
                uid=self.uid,
            )

        target_specs = self._discussion_target_specs(
            binding.learning_contract_version_id,
            binding.content_version_id,
        )
        session = AskMeDiscussionSession(
            id=self.uid("askme_discussion"),
            learning_run_id=learning_run.id,
            section_id=section_id,
            user_id=self.user_id,
            learning_contract_version_id=binding.learning_contract_version_id,
            content_version_id=binding.content_version_id,
            status="active",
            revision=0,
            active_topic_id="",
            pending_turn_id="",
            schema_version="ask_me_v2",
        )
        self.db.add(session)
        topics = []
        for position, spec in enumerate(
            self._topic_specs(context.section, target_specs)
        ):
            topic = AskMeDiscussionTopic(
                id=self.uid("askme_topic"),
                session_id=session.id,
                position=position,
                title=spec["title"],
                purpose=spec["purpose"],
                dimension=spec["dimension"],
                assessment_target_ids_json=self.dump([
                    spec["assessmentTargetId"]
                ]),
                status="active" if position == 0 else "pending",
                current_prompt=spec["prompt"],
                turn_count=0,
                evidence_recorded=False,
                final_assessment_json="{}",
            )
            self.db.add(topic)
            topics.append(topic)
        session.active_topic_id = topics[0].id
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self._discussion_session(learning_run.id, section_id)
            if existing:
                return self.discussion_view(existing)
            raise
        return self.discussion_view(session)

    async def submit_discussion_turn(
        self,
        section_id: str,
        body,
        idempotency_key: str,
    ):
        request_key = self._request_key(idempotency_key)
        answer = body.answer.strip()
        if not answer:
            raise AppError(
                "请先写下你的回答",
                code="ASK_ME_DISCUSSION_ANSWER_REQUIRED",
            )
        request_hash = self._request_hash({
            "sectionId": section_id,
            "sessionId": body.session_id,
            "topicId": body.topic_id,
            "expectedRevision": body.expected_revision,
            "answer": answer,
        })
        replay = self._discussion_turn_by_key(request_key)
        if (
            replay
            and replay.status != "failed"
            and not self._turn_lease_expired(replay)
        ):
            return self._turn_replay_or_retry(replay, request_hash, body)
        if replay and replay.request_hash != request_hash:
            raise AppError(
                "回答请求标识已用于其他内容",
                code="ASK_ME_DISCUSSION_IDEMPOTENCY_CONFLICT",
                status=409,
            )

        context, learning_run = self._discussion_context(section_id)
        session = self._locked_discussion_session(
            learning_run.id,
            section_id,
            body.session_id,
        )
        self._recover_expired_pending_turn(session)
        topic = self._validate_turn_submission(session, body)
        if replay:
            if (
                replay.session_id != session.id
                or replay.topic_id != topic.id
                or replay.turn_index != topic.turn_count
            ):
                raise AppError(
                    "上次失败的回答已不属于当前讨论位置",
                    code="ASK_ME_DISCUSSION_RETRY_CONFLICT",
                    status=409,
                )
            turn = replay
            turn.status = "processing"
            turn.error_code = ""
            turn.updated_at = now()
        else:
            turn = AskMeDiscussionTurnRecord(
                id=self.uid("askme_turn"),
                session_id=session.id,
                topic_id=topic.id,
                user_id=self.user_id,
                turn_index=topic.turn_count,
                prompt=topic.current_prompt,
                answer=answer,
                evaluation="",
                feedback_json="{}",
                status="processing",
                idempotency_key=request_key,
                request_hash=request_hash,
                response_json="",
                error_code="",
            )
            self.db.add(turn)
        lease_token = self.uid("askme_lease")
        turn.lease_token = lease_token
        turn.lease_expires_at = now() + self.DISCUSSION_TURN_LEASE
        session.pending_turn_id = turn.id
        session.updated_at = now()
        turn_id = turn.id
        session_id = session.id
        topic_id = topic.id
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self._discussion_turn_by_key(request_key)
            if replay:
                return self._turn_replay_or_retry(replay, request_hash, body)
            raise

        try:
            prior_turns = [
                self._discussion_turn_view(item)
                for item in self.db.scalars(
                    select(AskMeDiscussionTurnRecord)
                    .where(
                        AskMeDiscussionTurnRecord.topic_id == topic.id,
                        AskMeDiscussionTurnRecord.status == "completed",
                    )
                    .order_by(AskMeDiscussionTurnRecord.turn_index)
                )
            ]
            context_pack = self._generation_context(
                context,
                self._binding(learning_run.id, section_id),
                dimension=topic.dimension,
                prior_rounds=prior_turns,
                evaluates_dimension=topic.dimension,
                previous_prompt=turn.prompt,
                previous_answer=answer,
            )
            result = await self.tutor.ask_me_discussion(
                self.generation_contexts.attach(
                    {
                        "section": self.section_reader(section_id),
                        "discussionMode": "topic_v2",
                        "currentTopic": self._discussion_topic_view(topic),
                        "previousPrompt": turn.prompt,
                        "previousAnswer": answer,
                        "priorTurns": prior_turns,
                        **self._author_lineage(
                            self._binding(learning_run.id, section_id)
                        ),
                    },
                    context_pack,
                )
            )
            feedback = result.model_dump(mode="json")
            session = self.db.get(AskMeDiscussionSession, session_id)
            topic = self.db.get(AskMeDiscussionTopic, topic_id)
            turn = self.db.get(AskMeDiscussionTurnRecord, turn_id)
            if (
                not session
                or not topic
                or not turn
                or turn.status != "processing"
                or turn.lease_token != lease_token
                or session.pending_turn_id != turn.id
                or session.revision != body.expected_revision
                or session.active_topic_id != topic.id
            ):
                raise AppError(
                    "讨论状态已经变化，请刷新后继续",
                    code="ASK_ME_DISCUSSION_STATE_CHANGED",
                    status=409,
                )
            turn.evaluation = result.evaluation
            turn.feedback_json = self.dump(feedback)
            turn.status = "completed"
            turn.lease_token = ""
            turn.lease_expires_at = None
            turn.updated_at = now()
            topic.current_prompt = result.follow_up_prompt
            topic.turn_count += 1
            topic.status = (
                "sufficient"
                if result.topic_sufficiency == "sufficient"
                else "active"
            )
            topic.updated_at = now()
            session.pending_turn_id = ""
            session.revision += 1
            session.updated_at = now()
            response = self.discussion_view(session)
            turn.response_json = self.dump(response)
            self.db.commit()
            return response
        except BaseException as error:
            self.db.rollback()
            failed_turn = self.db.get(AskMeDiscussionTurnRecord, turn_id)
            failed_session = self.db.get(AskMeDiscussionSession, session_id)
            owns_lease = bool(
                failed_turn
                and failed_turn.status == "processing"
                and failed_turn.lease_token == lease_token
            )
            if owns_lease:
                failed_turn.status = "failed"
                failed_turn.error_code = safe_error_code(error)
                failed_turn.lease_token = ""
                failed_turn.lease_expires_at = None
                failed_turn.updated_at = now()
            if (
                owns_lease
                and failed_session
                and failed_session.pending_turn_id == turn_id
            ):
                failed_session.pending_turn_id = ""
                failed_session.updated_at = now()
            self.db.commit()
            raise

    def discussion_action(
        self,
        section_id: str,
        body,
        idempotency_key: str,
    ):
        request_key = self._request_key(idempotency_key)
        request_hash = self._request_hash({
            "sectionId": section_id,
            "sessionId": body.session_id,
            "expectedRevision": body.expected_revision,
            "action": body.action,
        })
        replay = self.db.scalar(
            select(AskMeDiscussionCommand).where(
                AskMeDiscussionCommand.user_id == self.user_id,
                AskMeDiscussionCommand.idempotency_key == request_key,
            )
        )
        if replay:
            if replay.request_hash != request_hash:
                raise AppError(
                    "讨论请求标识已用于其他操作",
                    code="ASK_ME_DISCUSSION_IDEMPOTENCY_CONFLICT",
                    status=409,
                )
            return self.load(replay.response_json, {})

        context, learning_run = self._discussion_context(section_id)
        session = self._locked_discussion_session(
            learning_run.id,
            section_id,
            body.session_id,
        )
        self._recover_expired_pending_turn(session)
        if session.revision != body.expected_revision:
            raise AppError(
                "讨论状态已经更新，请按当前进度继续",
                code="ASK_ME_DISCUSSION_REVISION_CONFLICT",
                status=409,
                details={"currentRevision": session.revision},
            )
        if session.pending_turn_id:
            raise AppError(
                "上一轮回答仍在评估，请稍候",
                code="ASK_ME_DISCUSSION_TURN_PROCESSING",
                status=409,
                retryable=True,
            )
        topic = self.db.get(AskMeDiscussionTopic, session.active_topic_id)
        if body.action == "resume":
            if session.status != "paused":
                raise AppError(
                    "当前讨论不需要恢复",
                    code="ASK_ME_DISCUSSION_NOT_PAUSED",
                    status=409,
                )
            session.status = "active"
        else:
            if session.status != "active":
                raise AppError(
                    "当前讨论不是进行中状态",
                    code="ASK_ME_DISCUSSION_NOT_ACTIVE",
                    status=409,
                )
            if body.action == "pause":
                session.status = "paused"
            elif body.action == "next_topic":
                if not topic:
                    raise AppError(
                        "当前主题不存在",
                        code="ASK_ME_DISCUSSION_TOPIC_NOT_FOUND",
                        status=409,
                    )
                next_topic = self.db.scalar(
                    select(AskMeDiscussionTopic)
                    .where(
                        AskMeDiscussionTopic.session_id == session.id,
                        AskMeDiscussionTopic.position > topic.position,
                    )
                    .order_by(AskMeDiscussionTopic.position)
                )
                if not next_topic:
                    raise AppError(
                        "已经是最后一个主题，请继续深入或结束讨论",
                        code="ASK_ME_DISCUSSION_NO_NEXT_TOPIC",
                        status=409,
                    )
                self._close_discussion_topic(context, session, topic)
                next_topic.status = "active"
                next_topic.updated_at = now()
                session.active_topic_id = next_topic.id
            elif body.action == "finish":
                if topic:
                    self._close_discussion_topic(context, session, topic)
                session.status = "completed"
                session.ended_at = now()
            else:
                raise AppError(
                    "不支持的讨论操作",
                    code="ASK_ME_DISCUSSION_ACTION_INVALID",
                )
        session.revision += 1
        session.updated_at = now()
        response = self.discussion_view(session)
        self.db.add(AskMeDiscussionCommand(
            id=self.uid("askme_command"),
            session_id=session.id,
            user_id=self.user_id,
            command_type=body.action,
            idempotency_key=request_key,
            request_hash=request_hash,
            response_json=self.dump(response),
        ))
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            replay = self.db.scalar(
                select(AskMeDiscussionCommand).where(
                    AskMeDiscussionCommand.user_id == self.user_id,
                    AskMeDiscussionCommand.idempotency_key == request_key,
                )
            )
            if replay and replay.request_hash == request_hash:
                return self.load(replay.response_json, {})
            raise
        return response

    def discussion_view(self, session):
        topics = list(self.db.scalars(
            select(AskMeDiscussionTopic)
            .where(AskMeDiscussionTopic.session_id == session.id)
            .order_by(AskMeDiscussionTopic.position)
        ))
        turns = list(self.db.scalars(
            select(AskMeDiscussionTurnRecord)
            .where(
                AskMeDiscussionTurnRecord.session_id == session.id,
                AskMeDiscussionTurnRecord.status == "completed",
            )
            .order_by(
                AskMeDiscussionTurnRecord.created_at,
                AskMeDiscussionTurnRecord.id,
            )
        ))
        return {
            "id": session.id,
            "status": session.status,
            "revision": session.revision,
            "activeTopicId": session.active_topic_id,
            "pending": bool(session.pending_turn_id),
            "schemaVersion": session.schema_version,
            "topics": [self._discussion_topic_view(topic) for topic in topics],
            "turns": [self._discussion_turn_view(turn) for turn in turns],
        }

    def view(self, session):
        entries = self.load(session.entries_json, [])
        return {
            "id": session.id,
            "status": session.status,
            "round": session.round_index + 1,
            "dimension": (
                entries[session.round_index]["dimension"]
                if entries
                else "mechanism"
            ),
            "prompt": (
                entries[session.round_index]["prompt"]
                if session.status != "completed" and entries
                else None
            ),
            "entries": entries,
        }

    async def _start(self, context, learning_run, binding, answer):
        if answer:
            raise AppError(
                "请先开始深入讨论再作答",
                code="ASK_ME_NOT_STARTED",
            )
        section_view = self.section_reader(context.section.id)
        target_spec = self._discussion_target_specs(
            binding.learning_contract_version_id,
            binding.content_version_id,
        )["mechanism"]
        context_pack = self._generation_context(
            context,
            binding,
            dimension="mechanism",
            prior_rounds=[],
        )
        self.db.commit()
        turn = None
        for validation_attempt in range(1, 4):
            turn = await self.tutor.ask_me(
                self.generation_contexts.attach(
                    {
                        "section": section_view,
                        "dimension": "mechanism",
                        "assessmentTarget": target_spec,
                        "previousAnswer": None,
                        "finalize": False,
                        "validationAttempt": validation_attempt,
                        "requiredEvaluation": "not_evaluated",
                        **self._author_lineage(binding),
                    },
                    context_pack,
                )
            )
            if (
                turn.dimension == "mechanism"
                and turn.evaluation == "not_evaluated"
            ):
                break
        if (
            turn is None
            or turn.dimension != "mechanism"
            or turn.evaluation != "not_evaluated"
        ):
            raise AiError("Ask Me 首轮结构无效")
        session = AskMeSession(
            id=self.uid("askme"),
            learning_run_id=learning_run.id,
            section_id=context.section.id,
            user_id=self.user_id,
            learning_contract_version_id=binding.learning_contract_version_id,
            content_version_id=binding.content_version_id,
            round_index=0,
            entries_json=self.dump(
                [
                    {
                        "dimension": "mechanism",
                        "prompt": turn.prompt,
                        "answer": None,
                        "evaluation": "not_evaluated",
                        "rationale": "",
                    }
                ]
            ),
        )
        self.db.add(session)
        self.db.commit()
        return self.view(session)

    async def _continue(self, context, binding, session, entries, answer):
        current = session.round_index
        current_dimension = self.DIMENSIONS[current]
        finalize = current == len(self.DIMENSIONS) - 1
        requested_dimension = (
            current_dimension if finalize else self.DIMENSIONS[current + 1]
        )
        target_specs = self._discussion_target_specs(
            binding.learning_contract_version_id,
            binding.content_version_id,
        )
        current_target = target_specs[current_dimension]
        requested_target = target_specs[requested_dimension]
        section_view = self.section_reader(context.section.id)
        context_pack = self._generation_context(
            context,
            binding,
            dimension=requested_dimension,
            prior_rounds=entries,
            evaluates_dimension=current_dimension,
            previous_prompt=entries[current]["prompt"],
            previous_answer=answer,
        )
        self.db.commit()
        turn = None
        for validation_attempt in range(1, 4):
            turn = await self.tutor.ask_me(
                self.generation_contexts.attach(
                    {
                        "section": section_view,
                        "dimension": requested_dimension,
                        "evaluatesDimension": current_dimension,
                        "assessmentTarget": requested_target,
                        "evaluatesAssessmentTarget": current_target,
                        "previousPrompt": entries[current]["prompt"],
                        "previousAnswer": answer,
                        "priorRounds": entries,
                        "finalize": finalize,
                        "validationAttempt": validation_attempt,
                        "requiredEvaluation": ["strong", "partial", "weak"],
                        **self._author_lineage(binding),
                    },
                    context_pack,
                )
            )
            if (
                turn.dimension == requested_dimension
                and turn.evaluation != "not_evaluated"
            ):
                break
        if turn is None or turn.evaluation == "not_evaluated":
            raise AiError("Ask Me 作答后必须给出能力评估")
        entries[current].update(
            {
                "answer": answer,
                "evaluation": turn.evaluation,
                "rationale": turn.rationale,
            }
        )
        delta = {"strong": 20, "partial": 8, "weak": -5}[turn.evaluation]
        self.evidence_recorder(
            self.evidence_context(context.section),
            f"{context.section.title}:{current_dimension}",
            "ask_me",
            {
                "sessionId": session.id,
                "dimension": current_dimension,
                "evaluation": turn.evaluation,
                "assessmentTargetIds": [current_target["assessmentTargetId"]],
                "learningContractVersionId": (
                    binding.learning_contract_version_id
                ),
                "contentVersionId": binding.content_version_id,
            },
            delta,
        )
        if finalize:
            session.status = "completed"
        else:
            if turn.dimension != requested_dimension:
                raise AiError("Ask Me 轮次顺序无效")
            entries.append(
                {
                    "dimension": requested_dimension,
                    "prompt": turn.prompt,
                    "answer": None,
                    "evaluation": "not_evaluated",
                    "rationale": "",
                }
            )
            session.round_index += 1
        session.entries_json = self.dump(entries)
        session.updated_at = now()
        self.db.commit()
        return self.view(session)

    def _binding(self, learning_run_id: str, section_id: str):
        return self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run_id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )

    def _discussion_target_specs(
        self,
        contract_version_id: str | None,
        content_version_id: str | None,
    ) -> dict[str, dict[str, str]]:
        if not contract_version_id:
            raise AppError(
                "深入讨论缺少冻结的学习契约",
                code="ASK_ME_CONTRACT_MISSING",
                status=409,
            )
        if not content_version_id:
            raise AppError(
                "深入讨论缺少冻结的正文版本",
                code="ASK_ME_CONTENT_VERSION_MISSING",
                status=409,
            )
        rows = self.db.execute(
            select(
                LearningContractAssessmentTarget.position,
                AssessmentTarget.id,
                AssessmentTarget.objective_statement,
                AssessmentTarget.dimension,
                ContentBlockVersion.semantic_role,
                ContentBlockVersion.position,
            )
            .join(
                AssessmentTarget,
                AssessmentTarget.id
                == LearningContractAssessmentTarget.assessment_target_id,
            )
            .join(
                ContentBlockAssessmentTarget,
                ContentBlockAssessmentTarget.assessment_target_id
                == AssessmentTarget.id,
            )
            .join(
                ContentBlockVersion,
                ContentBlockVersion.id
                == ContentBlockAssessmentTarget.content_block_version_id,
            )
            .where(
                LearningContractAssessmentTarget.contract_version_id
                == contract_version_id,
                ContentBlockVersion.content_version_id == content_version_id,
            )
            .order_by(
                LearningContractAssessmentTarget.position,
                ContentBlockVersion.position,
            )
        ).all()
        candidates_by_role: dict[str, list[dict[str, str]]] = {}
        all_candidates: list[dict[str, str]] = []
        for _, target_id, objective, target_dimension, role, _ in rows:
            candidates = candidates_by_role.setdefault(role, [])
            if any(item["assessmentTargetId"] == target_id for item in candidates):
                continue
            candidate = {
                "assessmentTargetId": target_id,
                "objective": objective,
                "dimension": target_dimension,
            }
            candidates.append(candidate)
            if not any(item["assessmentTargetId"] == target_id for item in all_candidates):
                all_candidates.append(candidate)

        # M1 content keeps the same explicit block-to-objective declarations in
        # the immutable block payload instead of the normalized binding table.
        # Accept only exact target IDs or exact frozen objective statements; do
        # not infer a target from prose or fuzzy similarity.
        content = self.db.get(ContentVersion, content_version_id)
        contract_targets = self.db.execute(
            select(
                AssessmentTarget.id,
                AssessmentTarget.objective_statement,
                AssessmentTarget.dimension,
            )
            .join(
                LearningContractAssessmentTarget,
                LearningContractAssessmentTarget.assessment_target_id
                == AssessmentTarget.id,
            )
            .where(
                LearningContractAssessmentTarget.contract_version_id
                == contract_version_id
            )
            .order_by(LearningContractAssessmentTarget.position)
        ).all()
        target_by_id = {
            target_id: {
                "assessmentTargetId": target_id,
                "objective": objective,
                "dimension": dimension,
            }
            for target_id, objective, dimension in contract_targets
        }
        targets_by_objective: dict[str, list[dict[str, str]]] = {}
        for target_id, objective, _dimension in contract_targets:
            targets_by_objective.setdefault(objective, []).append(
                target_by_id[target_id]
            )
        if content:
            for block in self.load(content.blocks_json, []):
                role = str(block.get("role", ""))
                if not role:
                    continue
                raw_target_ids = [
                    str(item)
                    for item in block.get("assessmentTargetIds", [])
                    if str(item)
                ]
                if any(item not in target_by_id for item in raw_target_ids):
                    raise AppError(
                        "正文目标绑定超出冻结的学习契约",
                        code="ASK_ME_CONTENT_TARGET_BOUNDARY_INVALID",
                        status=409,
                        details={"blockId": str(block.get("id", ""))},
                    )
                declared_ids = raw_target_ids
                if not declared_ids:
                    for objective in block.get("assessment_objectives", []):
                        matches = targets_by_objective.get(objective, [])
                        if len(matches) == 1:
                            declared_ids.append(
                                matches[0]["assessmentTargetId"]
                            )
                candidates = candidates_by_role.setdefault(role, [])
                for target_id in declared_ids:
                    candidate = target_by_id[target_id]
                    if candidate not in candidates:
                        candidates.append(candidate)
                    if candidate not in all_candidates:
                        all_candidates.append(candidate)

        assigned: set[str] = set()
        result: dict[str, dict[str, str]] = {}
        for dimension in self.DIMENSIONS:
            candidates: list[dict[str, str]] = []
            matching_dimensions = {
                "mechanism": {"mechanism", "recognition"},
                "boundary": {"boundary", "comparison"},
                "transfer": {"transfer", "application"},
            }[dimension]
            for candidate in all_candidates:
                if candidate.get("dimension") in matching_dimensions:
                    candidates.append(candidate)
            for role in self.TARGET_BLOCK_ROLES[dimension]:
                for candidate in candidates_by_role.get(role, []):
                    if candidate not in candidates:
                        candidates.append(candidate)
            for candidate in all_candidates:
                if candidate not in candidates:
                    candidates.append(candidate)
            if not candidates:
                raise AppError(
                    "正文没有为深入讨论提供可验证的目标绑定",
                    code="ASK_ME_TOPIC_TARGETS_MISSING",
                    status=409,
                    details={"dimension": dimension},
                )
            selected = next(
                (
                    item
                    for item in candidates
                    if item["assessmentTargetId"] not in assigned
                ),
                candidates[0],
            )
            result[dimension] = selected
            assigned.add(selected["assessmentTargetId"])
        return result

    def _session(self, learning_run_id: str, section_id: str):
        return self.db.scalar(
            select(AskMeSession).where(
                AskMeSession.section_id == section_id,
                AskMeSession.user_id == self.user_id,
                AskMeSession.learning_run_id == learning_run_id,
            )
        )

    def _discussion_context(self, section_id: str):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        if not self.progress.for_section(
            context.section,
            context.chapter,
            context.book,
        ).ask_me_unlocked:
            raise AppError(
                "小节满分后才解锁深入讨论",
                code="ASK_ME_LOCKED",
                status=403,
            )
        return context, self.progress.active_run(context.series.id)

    def _discussion_session(self, learning_run_id: str, section_id: str):
        return self.db.scalar(
            select(AskMeDiscussionSession).where(
                AskMeDiscussionSession.learning_run_id == learning_run_id,
                AskMeDiscussionSession.section_id == section_id,
                AskMeDiscussionSession.user_id == self.user_id,
            )
        )

    def _locked_discussion_session(
        self,
        learning_run_id: str,
        section_id: str,
        session_id: str,
    ):
        session = self.db.scalar(
            select(AskMeDiscussionSession)
            .where(
                AskMeDiscussionSession.id == session_id,
                AskMeDiscussionSession.learning_run_id == learning_run_id,
                AskMeDiscussionSession.section_id == section_id,
                AskMeDiscussionSession.user_id == self.user_id,
            )
            .with_for_update()
        )
        if not session:
            raise AppError(
                "深入讨论会话不存在",
                code="ASK_ME_DISCUSSION_NOT_FOUND",
                status=404,
            )
        return session

    def _validate_turn_submission(self, session, body):
        if session.status != "active":
            raise AppError(
                "请先恢复讨论再继续作答",
                code="ASK_ME_DISCUSSION_NOT_ACTIVE",
                status=409,
            )
        if session.revision != body.expected_revision:
            raise AppError(
                "讨论状态已经更新，请按当前问题继续",
                code="ASK_ME_DISCUSSION_REVISION_CONFLICT",
                status=409,
                details={"currentRevision": session.revision},
            )
        if session.pending_turn_id:
            raise AppError(
                "上一轮回答仍在评估，请稍候",
                code="ASK_ME_DISCUSSION_TURN_PROCESSING",
                status=409,
                retryable=True,
            )
        topic = self.db.get(AskMeDiscussionTopic, body.topic_id)
        if (
            not topic
            or topic.session_id != session.id
            or session.active_topic_id != topic.id
            or topic.status not in {"active", "sufficient"}
        ):
            raise AppError(
                "当前主题已经变化，请刷新后继续",
                code="ASK_ME_DISCUSSION_TOPIC_CONFLICT",
                status=409,
            )
        return topic

    def _discussion_turn_by_key(self, request_key: str):
        return self.db.scalar(
            select(AskMeDiscussionTurnRecord).where(
                AskMeDiscussionTurnRecord.user_id == self.user_id,
                AskMeDiscussionTurnRecord.idempotency_key == request_key,
            )
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _turn_lease_expired(self, turn) -> bool:
        return bool(
            turn.status == "processing"
            and turn.lease_expires_at
            and self._utc(turn.lease_expires_at) <= now()
        )

    def _recover_expired_pending_turn(self, session) -> None:
        if not session.pending_turn_id:
            return
        pending = self.db.get(
            AskMeDiscussionTurnRecord,
            session.pending_turn_id,
        )
        if pending and pending.status == "processing":
            if not self._turn_lease_expired(pending):
                return
            pending.status = "failed"
            pending.error_code = "ASK_ME_DISCUSSION_TURN_LEASE_EXPIRED"
            pending.lease_token = ""
            pending.lease_expires_at = None
            pending.updated_at = now()
        session.pending_turn_id = ""
        session.updated_at = now()

    def _turn_replay_or_retry(self, turn, request_hash: str, body):
        if turn.request_hash != request_hash:
            raise AppError(
                "回答请求标识已用于其他内容",
                code="ASK_ME_DISCUSSION_IDEMPOTENCY_CONFLICT",
                status=409,
            )
        if turn.status == "completed" and turn.response_json:
            return self.load(turn.response_json, {})
        if turn.status == "processing":
            raise AppError(
                "这份回答仍在评估，请稍候",
                code="ASK_ME_DISCUSSION_TURN_PROCESSING",
                status=409,
                retryable=True,
            )
        raise AppError(
            "上次评估没有完成，请重新提交",
            code="ASK_ME_DISCUSSION_TURN_RETRY_REQUIRED",
            status=409,
            retryable=True,
            details={"expectedRevision": body.expected_revision},
        )

    def _topic_specs(self, section, target_specs):
        anchor = section.question or section.title
        mechanism = target_specs["mechanism"]["objective"]
        boundary = target_specs["boundary"]["objective"]
        transfer = target_specs["transfer"]["objective"]
        return [
            {
                "dimension": "mechanism",
                "assessmentTargetId": target_specs["mechanism"][
                    "assessmentTargetId"
                ],
                "title": "把核心机制讲清楚",
                "purpose": f"验证目标“{mechanism}”背后的因果链和判断依据。",
                "prompt": (
                    f"围绕“{anchor}”，先用自己的话说明“{mechanism}”。"
                    "请把结论、关键机制和你依据的可观察信号连接起来。"
                ),
            },
            {
                "dimension": "boundary",
                "assessmentTargetId": target_specs["boundary"][
                    "assessmentTargetId"
                ],
                "title": "找到判断失效的边界",
                "purpose": f"验证目标“{boundary}”在什么条件下不再成立。",
                "prompt": (
                    f"围绕目标“{boundary}”，举出一个容易误判的边界情形。"
                    "你会用什么证据区分它和正常情况？"
                ),
            },
            {
                "dimension": "transfer",
                "assessmentTargetId": target_specs["transfer"][
                    "assessmentTargetId"
                ],
                "title": "迁移到新的真实情境",
                "purpose": f"把目标“{transfer}”迁移到新的职业或现实场景。",
                "prompt": (
                    f"请选择一个正文未直接出现的新场景，应用“{transfer}”的判断方法。"
                    "请说明你的步骤、证据和可能失效的地方。"
                ),
            },
        ]

    def _discussion_topic_view(self, topic):
        return {
            "id": topic.id,
            "position": topic.position,
            "title": topic.title,
            "purpose": topic.purpose,
            "dimension": topic.dimension,
            "assessmentTargetIds": self.load(
                topic.assessment_target_ids_json,
                [],
            ),
            "status": topic.status,
            "currentPrompt": topic.current_prompt,
            "turnCount": topic.turn_count,
            "evidenceRecorded": topic.evidence_recorded,
            "finalAssessment": self.load(topic.final_assessment_json, {}),
        }

    def _discussion_turn_view(self, turn):
        feedback = self.load(turn.feedback_json, {})
        return {
            "id": turn.id,
            "topicId": turn.topic_id,
            "turnIndex": turn.turn_index,
            "prompt": turn.prompt,
            "answer": turn.answer,
            "evaluation": turn.evaluation,
            "feedback": {
                "evaluation": feedback.get("evaluation", turn.evaluation),
                "correctPoints": feedback.get("correct_points", []),
                "issues": [
                    {
                        "kind": issue.get("kind", "reasoning_gap"),
                        "answerExcerpt": issue.get("answer_excerpt", ""),
                        "explanation": issue.get("explanation", ""),
                    }
                    for issue in feedback.get("issues", [])
                ],
                "suggestions": feedback.get("suggestions", []),
                "followUpPrompt": feedback.get("follow_up_prompt", ""),
                "followUpPurpose": feedback.get("follow_up_purpose", ""),
                "topicSufficiency": feedback.get(
                    "topic_sufficiency",
                    "insufficient",
                ),
            },
            "createdAt": turn.created_at.isoformat(),
        }

    def _close_discussion_topic(self, context, session, topic):
        latest_turn = self.db.scalar(
            select(AskMeDiscussionTurnRecord)
            .where(
                AskMeDiscussionTurnRecord.topic_id == topic.id,
                AskMeDiscussionTurnRecord.status == "completed",
            )
            .order_by(AskMeDiscussionTurnRecord.turn_index.desc())
        )
        topic.status = "closed"
        topic.updated_at = now()
        if not latest_turn or topic.evidence_recorded:
            return
        feedback = self.load(latest_turn.feedback_json, {})
        topic.final_assessment_json = self.dump({
            "turnId": latest_turn.id,
            "evaluation": latest_turn.evaluation,
            "feedback": feedback,
        })
        self.evidence_recorder(
            self.evidence_context(context.section),
            f"{context.section.title}:{topic.title}",
            "ask_me_topic",
            {
                "sessionId": session.id,
                "topicId": topic.id,
                "dimension": topic.dimension,
                "evaluation": latest_turn.evaluation,
                "turnCount": topic.turn_count,
                "assessmentTargetIds": self.load(
                    topic.assessment_target_ids_json,
                    [],
                ),
                "learningContractVersionId": (
                    session.learning_contract_version_id
                ),
                "contentVersionId": session.content_version_id,
            },
            {"strong": 20, "partial": 8, "weak": -5}[latest_turn.evaluation],
        )
        topic.evidence_recorded = True

    @staticmethod
    def _request_key(value: str):
        request_key = value.strip()
        if not 8 <= len(request_key) <= 128:
            raise AppError(
                "回答请求标识无效",
                code="IDEMPOTENCY_KEY_INVALID",
                status=400,
            )
        return request_key

    @staticmethod
    def _request_hash(payload: dict):
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _generation_context(
        self,
        context,
        binding,
        *,
        dimension: str,
        prior_rounds: list,
        evaluates_dimension: str | None = None,
        previous_prompt: str | None = None,
        previous_answer: str | None = None,
    ):
        contract = self.db.get(
            LearningContractVersion,
            binding.learning_contract_version_id,
        )
        mission = (
            self.db.get(LearningMissionVersion, contract.mission_version_id)
            if contract
            else None
        )
        interaction = {
            "dimension": dimension,
            "priorRounds": prior_rounds,
        }
        if evaluates_dimension is not None:
            interaction.update(
                {
                    "evaluatesDimension": evaluates_dimension,
                    "previousPrompt": previous_prompt,
                    "previousAnswer": previous_answer,
                }
            )
        return self.generation_contexts.build(
            "ask_me",
            shelf=context.shelf,
            series=context.series,
            book=context.book,
            chapter=context.chapter,
            section=context.section,
            mission=mission,
            contract=contract,
            memory=self.memory_loader(context.book.shelf_id, 10),
            interaction=interaction,
        )
