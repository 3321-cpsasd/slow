from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.generation_context import GenerationContextBuilder
from ...core.errors import AiError, AppError
from ...infrastructure.tables import (
    AskMeSession,
    LearningContractVersion,
    LearningMissionVersion,
    LearningRunSectionBinding,
    now,
)
from ..learning.contracts import open_run_section
from ..learning.progress import ProgressStore


class AskMeService:
    """Runs the three-stage adaptive oral assessment after a perfect quiz."""

    DIMENSIONS = ("mechanism", "boundary", "transfer")

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
                        "previousAnswer": None,
                        "finalize": False,
                        "validationAttempt": validation_attempt,
                        "requiredEvaluation": "not_evaluated",
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
                        "previousPrompt": entries[current]["prompt"],
                        "previousAnswer": answer,
                        "priorRounds": entries,
                        "finalize": finalize,
                        "validationAttempt": validation_attempt,
                        "requiredEvaluation": ["strong", "partial", "weak"],
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
            {"dimension": current_dimension, "evaluation": turn.evaluation},
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

    def _session(self, learning_run_id: str, section_id: str):
        return self.db.scalar(
            select(AskMeSession).where(
                AskMeSession.section_id == section_id,
                AskMeSession.user_id == self.user_id,
                AskMeSession.learning_run_id == learning_run_id,
            )
        )

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
