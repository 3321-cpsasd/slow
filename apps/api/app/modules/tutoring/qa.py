from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.generation_context import GenerationContextBuilder
from ...core.errors import AiError, AppError
from ...infrastructure.tables import (
    LearningContractVersion,
    LearningMissionVersion,
    LearningRunSectionBinding,
    QaMessage,
    QaSession,
    QaThread,
    now,
)
from ..learning.contracts import open_run_section
from ..learning.progress import ProgressStore


class QaService:
    """Owns paragraph-bound Ask AI sessions, threads, and classification repair."""

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
        self.uid = uid
        self.dump = dump
        self.load = load

    def prepare(self, section_id: str, body):
        section_context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(section_context.series.id)
        binding = self.db.scalar(
            select(LearningRunSectionBinding).where(
                LearningRunSectionBinding.learning_run_id == learning_run.id,
                LearningRunSectionBinding.user_id == self.user_id,
                LearningRunSectionBinding.section_id == section_id,
            )
        )
        if not binding:
            mission = self.missions.current_version(section_context.series.id)
            binding = open_run_section(
                self.db,
                run=learning_run,
                section=section_context.section,
                mission_version_id=mission.id,
                source="ask_ai_block_recovery",
                uid=self.uid,
                preferred_block_id=body.block_id,
            )
            self.db.commit()
        section_view = self.section_reader(section_id)
        if not section_view["content"]:
            raise AppError("请先生成本节", code="SECTION_NOT_GENERATED")
        valid_blocks = {item["id"] for item in section_view["content"]["blocks"]}
        if body.block_id not in valid_blocks:
            raise AppError(
                "内容块不存在或版本已失效",
                code="BLOCK_INVALID",
                status=409,
            )
        session = self.db.scalar(
            select(QaSession).where(
                QaSession.section_id == section_id,
                QaSession.user_id == self.user_id,
                QaSession.learning_run_id == learning_run.id,
            )
        )
        if not session:
            session = QaSession(
                id=self.uid("qa"),
                learning_run_id=learning_run.id,
                section_id=section_id,
                user_id=self.user_id,
                learning_contract_version_id=(
                    binding.learning_contract_version_id
                ),
                content_version_id=binding.content_version_id,
            )
            self.db.add(session)
            self.db.commit()
        messages = self.db.scalars(
            select(QaMessage)
            .where(QaMessage.session_id == session.id)
            .order_by(QaMessage.created_at)
        ).all()
        threads = self.db.scalars(
            select(QaThread)
            .where(QaThread.session_id == session.id)
            .order_by(QaThread.updated_at.desc())
        ).all()
        current_history = [
            {
                "role": item.role,
                "content": item.content,
                "blockId": item.block_id,
            }
            for item in messages
            if body.thread_id and item.thread_id == body.thread_id
        ]
        related_summaries = [
            {"threadId": item.thread_id, "summary": item.summary}
            for item in threads
            if item.thread_id != body.thread_id and item.summary
        ][:5]
        suggested = self.uid("thread")
        contract = self.db.get(
            LearningContractVersion,
            binding.learning_contract_version_id,
        )
        mission = (
            self.db.get(LearningMissionVersion, contract.mission_version_id)
            if contract
            else None
        )
        cross_section_memory = self.memory_loader(
            section_context.book.shelf_id,
            10,
        )
        context_pack = self.generation_contexts.build(
            "ask_ai",
            shelf=section_context.shelf,
            series=section_context.series,
            book=section_context.book,
            chapter=section_context.chapter,
            section=section_context.section,
            mission=mission,
            contract=contract,
            memory=cross_section_memory,
            interaction={
                "anchorBlockId": body.block_id,
                "question": body.question,
                "currentThreadFullHistory": current_history,
                "relatedThreadSummaries": related_summaries,
            },
        )
        request = {
            "section": section_view,
            "anchorBlockId": body.block_id,
            "question": body.question,
            "requestedThreadId": body.thread_id,
            "forcedRelation": body.force_relation,
            "newThreadId": suggested,
            "weightedContext": {
                "currentThreadFullHistory": current_history,
                "relatedThreadSummaries": related_summaries,
                "crossSectionMemory": cross_section_memory,
            },
        }
        return {
            "session": session,
            "suggestedThreadId": suggested,
            "request": self.generation_contexts.attach(request, context_pack),
        }

    def save_answer(
        self,
        context,
        body,
        answer: str,
        suggested_relation: str,
        thread_summary: str = "",
    ):
        session = context["session"]
        suggested = context["suggestedThreadId"]
        relation = body.force_relation or suggested_relation
        if relation == "follow_up" and body.thread_id:
            thread_id = body.thread_id
        else:
            relation, thread_id = "new_question", suggested
        thread = self.db.scalar(
            select(QaThread).where(
                QaThread.session_id == session.id,
                QaThread.thread_id == thread_id,
            )
        )
        if not thread:
            thread = QaThread(
                id=self.uid("qathread"),
                session_id=session.id,
                thread_id=thread_id,
                classification=relation,
            )
            self.db.add(thread)
        thread_summary = thread_summary.strip() or answer.strip()[:240]
        thread.summary = thread_summary
        thread.updated_at = now()
        self.db.add_all(
            [
                QaMessage(
                    id=self.uid("msg"),
                    session_id=session.id,
                    thread_id=thread_id,
                    block_id=body.block_id,
                    role="user",
                    content=body.question,
                ),
                QaMessage(
                    id=self.uid("msg"),
                    session_id=session.id,
                    thread_id=thread_id,
                    block_id=body.block_id,
                    role="assistant",
                    content=answer,
                ),
            ]
        )
        memory = self.load(session.memory_json, {"threads": {}}) or {
            "threads": {}
        }
        memory.setdefault("threads", {})[thread_id] = thread_summary
        memory["lastThread"] = thread_id
        session.memory_json = self.dump(memory)
        self.db.commit()
        return {
            "sessionId": session.id,
            "threadId": thread_id,
            "relation": relation,
            "answer": answer,
            "classificationCorrectable": True,
        }

    async def ask(self, section_id: str, body):
        context = self.prepare(section_id, body)
        self.db.commit()
        result = await self.tutor.answer(context["request"])
        return self.save_answer(
            context,
            body,
            result.answer,
            result.relation,
            result.thread_summary,
        )

    async def stream(self, context, body):
        parts = []
        self.db.commit()
        stream_answer = getattr(self.tutor, "answer_stream", None)
        if callable(stream_answer):
            async for delta in stream_answer(context["request"]):
                if delta:
                    parts.append(delta)
                    yield {"type": "delta", "delta": delta}
            suggested_relation = (
                "follow_up" if body.thread_id else "new_question"
            )
        else:
            result = await self.tutor.answer(context["request"])
            parts.append(result.answer)
            suggested_relation = result.relation
            yield {"type": "delta", "delta": result.answer}
        answer = "".join(parts).strip()
        if not answer:
            raise AiError("答疑模型未返回有效内容")
        saved = self.save_answer(context, body, answer, suggested_relation)
        yield {
            "type": "done",
            "sessionId": saved["sessionId"],
            "threadId": saved["threadId"],
            "relation": saved["relation"],
            "classificationCorrectable": True,
        }

    def correct_classification(self, section_id: str, thread_id: str, body):
        context = self.contexts.resolve_section(
            user_id=self.user_id,
            section_id=section_id,
        )
        learning_run = self.progress.active_run(context.series.id)
        session = self.db.scalar(
            select(QaSession).where(
                QaSession.section_id == section_id,
                QaSession.user_id == self.user_id,
                QaSession.learning_run_id == learning_run.id,
            )
        )
        thread = (
            self.db.scalar(
                select(QaThread).where(
                    QaThread.session_id == session.id,
                    QaThread.thread_id == thread_id,
                )
            )
            if session
            else None
        )
        if not thread:
            raise AppError(
                "答疑线程不存在",
                code="QA_THREAD_NOT_FOUND",
                status=404,
            )
        if body.relation == "follow_up":
            target = (
                self.db.scalar(
                    select(QaThread).where(
                        QaThread.session_id == session.id,
                        QaThread.thread_id == body.target_thread_id,
                    )
                )
                if body.target_thread_id
                else None
            )
            if not target or target.thread_id == thread_id:
                raise AppError(
                    "纠正为追问时必须指定另一条已有线程",
                    code="QA_TARGET_INVALID",
                )
            messages = self.db.scalars(
                select(QaMessage).where(
                    QaMessage.session_id == session.id,
                    QaMessage.thread_id == thread_id,
                )
            ).all()
            for item in messages:
                item.thread_id = target.thread_id
            target.summary = "；".join(
                value for value in [target.summary, thread.summary] if value
            )
            target.corrected = True
            target.updated_at = now()
            self.db.delete(thread)
            corrected_id = target.thread_id
        else:
            thread.classification = "new_question"
            thread.corrected = True
            thread.updated_at = now()
            corrected_id = thread.thread_id
        self.db.commit()
        return {
            "threadId": corrected_id,
            "relation": body.relation,
            "corrected": True,
        }
