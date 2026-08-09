import json
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...ai.port import AiPort
from ...core.errors import AppError
from ...infrastructure.tables import ChapterPractice, Section
from ..knowledge.fact_graph import KnowledgeFactGraphService
from ..learning.generation_leases import (
    acquire_generation_lease,
    release_generation_lease,
    renew_generation_lease,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def section_objectives_payload(item) -> list:
    """Freeze model-selected allowlisted identity keys with each section objective."""

    if not item.baseline_concept_key:
        return list(item.objectives)
    return [
        {
            "statement": statement,
            "required": objective_position == 1,
            "baselineConceptKey": item.baseline_concept_key,
            "baselineObjectiveKey": item.baseline_objective_key,
        }
        for objective_position, statement in enumerate(item.objectives, 1)
    ]


class ChapterPlanningService:
    """Plans a confirmed chapter's sections without owning lesson generation.

    This boundary is intentionally separate from Series route planning and
    per-section publication. It may create Section rows and the chapter
    practice atomically, but it cannot change the confirmed Chapter semantics.
    """

    def __init__(
        self,
        db: Session,
        ai: AiPort,
        *,
        user_id: str,
        contexts,
        progress,
        artifacts,
        missions,
        generation_contexts,
        memory_provider: Callable[[str], list[dict]],
        chapter_view: Callable,
    ):
        self.db = db
        self.ai = ai
        self.user_id = user_id
        self.contexts = contexts
        self.progress = progress
        self.artifacts = artifacts
        self.missions = missions
        self.generation_contexts = generation_contexts
        self.memory_provider = memory_provider
        self.chapter_view = chapter_view

    async def generate(
        self,
        chapter_id: str,
        *,
        first_section_status: str = "available",
    ) -> dict:
        if first_section_status not in {"available", "preparing"}:
            raise AppError(
                "首节准备状态无效",
                code="SECTION_PREPARATION_STATUS_INVALID",
                status=500,
            )
        chapter_context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        if self._has_sections(chapter_id):
            return self.chapter_view(chapter_context.chapter)

        resource_key = f"chapter:{chapter_id}"
        owner_id = acquire_generation_lease(self.db, resource_key)
        if owner_id is None:
            raise AppError(
                "本章正在生成，请等待当前任务完成",
                code="GENERATION_IN_PROGRESS",
                status=409,
            )
        try:
            return await self._generate_locked(
                chapter_id,
                resource_key=resource_key,
                owner_id=owner_id,
                first_section_status=first_section_status,
            )
        finally:
            release_generation_lease(self.db, resource_key, owner_id)

    def _has_sections(self, chapter_id: str) -> bool:
        return bool(
            self.db.scalar(
                select(func.count())
                .select_from(Section)
                .where(Section.chapter_id == chapter_id)
            )
        )

    def _renew_lease(self, resource_key: str, owner_id: str) -> None:
        if not renew_generation_lease(self.db, resource_key, owner_id):
            raise AppError(
                "生成租约已经被新的请求接管，旧结果不会保存",
                code="GENERATION_LEASE_LOST",
                status=409,
            )

    async def _generate_locked(
        self,
        chapter_id: str,
        *,
        resource_key: str,
        owner_id: str,
        first_section_status: str,
    ) -> dict:
        chapter_context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        chapter = chapter_context.chapter
        if chapter_context.book.outline_status != "confirmed":
            raise AppError(
                "本书章节仍是草案，请先按最新学习画像校准并确认",
                code="BOOK_OUTLINE_CONFIRMATION_REQUIRED",
                status=409,
            )
        if self.progress.for_chapter(chapter, chapter_context.book).status == "locked":
            raise AppError("请先完成前置学习", code="CHAPTER_LOCKED", status=403)
        if self._has_sections(chapter.id):
            return self.chapter_view(chapter)

        book = chapter_context.book
        memory = self.memory_provider(book.shelf_id)
        mission = self.missions.current_version(chapter_context.series.id)
        knowledge_identities = KnowledgeFactGraphService(
            self.db
        ).chapter_identity_allowlist(chapter.id)
        context_pack = self.generation_contexts.build(
            "chapter",
            shelf=chapter_context.shelf,
            series=chapter_context.series,
            book=book,
            chapter=chapter,
            mission=mission,
            memory=memory,
        )
        request = self.generation_contexts.attach(
            {
                "title": chapter.title,
                "objective": chapter.objective,
                "knowledgeIdentityAllowlist": knowledge_identities,
            },
            context_pack,
        )

        # Release the transaction before the remote call. The generation lease
        # remains the cross-request authority for this chapter.
        self.db.commit()
        generated = await self.ai.chapter(request, memory)
        self._renew_lease(resource_key, owner_id)
        KnowledgeFactGraphService(self.db).validate_chapter_outline_identities(
            chapter.id,
            generated.sections,
        )

        chapter_context = self.contexts.resolve_chapter(
            user_id=self.user_id,
            chapter_id=chapter_id,
        )
        chapter = chapter_context.chapter
        if self._has_sections(chapter.id):
            self.db.commit()
            return self.chapter_view(chapter)

        run = self.progress.active_run(chapter_context.series.id)
        for position, item in enumerate(generated.sections, 1):
            section = Section(
                id=_uid("section"),
                chapter_id=chapter.id,
                position=position,
                title=item.title,
                question=item.question,
                objectives_json=_dump(section_objectives_payload(item)),
            )
            self.db.add(section)
            self.progress.add_section(
                run,
                section,
                status=first_section_status if position == 1 else "locked",
            )

        practice = ChapterPractice(
            id=_uid("practice"),
            chapter_id=chapter.id,
            title=f"{chapter.title}：章末实践",
            instructions_json=_dump(
                {
                    "objective": chapter.objective,
                    "steps": [
                        "完成一个最小实践",
                        "保存输入、输出或截图证据",
                        "记录失败边界与复盘",
                    ],
                }
            ),
        )
        self.db.add(practice)
        self.artifacts.add(
            learning_run_id=run.id,
            target_type="chapter_practice",
            target_id=practice.id,
        )
        self.db.commit()
        return self.chapter_view(chapter)
