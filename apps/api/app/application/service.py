import hashlib
import json
import re
from urllib.parse import unquote
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..ai.port import AiPort
from ..core.errors import AiError, AppError
from ..domain.learning import grade_choice_quiz
from ..infrastructure.tables import (
    ArtifactAttachment,
    AskMeSession,
    Book,
    BookCapstone,
    Chapter,
    ChapterPractice,
    ChapterRevision,
    ContentVersion,
    GenerationRun,
    LearningEvidence,
    LearningMemory,
    LearningNote,
    LearningPlan,
    PlanCreationRequest,
    QaMessage,
    QaSession,
    QaThread,
    QuizAttempt,
    QuizSet,
    Remediation,
    Section,
    Series,
    Shelf,
    SourceVerification,
    User,
    now,
)

USER_ID = "user_demo"
EXPECTED_SECTIONS_PER_CHAPTER = 4


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


def normalized(value: str):
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


class SlowService:
    def __init__(self, db: Session, ai: AiPort, source_verifier, attachment_storage=None):
        self.db, self.ai, self.source_verifier = db, ai, source_verifier
        self.attachment_storage = attachment_storage

    def ensure_seed(self):
        if not self.db.get(User, USER_ID):
            self.db.add(User(id=USER_ID, name="学习者"))
            self.db.add(
                Shelf(
                    id="shelf_technology",
                    user_id=USER_ID,
                    name="技术",
                    domain="计算机",
                    specialty="软件工程",
                    tags_json='["AI","云原生"]',
                )
            )
            self.db.commit()

    def shelf(self, shelf_id):
        row = self.db.scalar(select(Shelf).where(Shelf.id == shelf_id, Shelf.user_id == USER_ID))
        if not row:
            raise AppError("书架不存在", code="SHELF_NOT_FOUND", status=404)
        return row

    def bootstrap(self):
        shelves = self.db.scalars(select(Shelf).where(Shelf.user_id == USER_ID)).all()
        return {
            "user": {"id": USER_ID, "name": self.db.get(User, USER_ID).name},
            "shelves": [self._shelf(item) for item in shelves],
        }

    def _shelf(self, shelf):
        series = self.db.scalars(
            select(Series).where(Series.shelf_id == shelf.id, Series.deleted_at.is_(None))
        ).all()
        return {
            "id": shelf.id,
            "name": shelf.name,
            "domain": shelf.domain,
            "specialty": shelf.specialty,
            "tags": load(shelf.tags_json, []),
            "series": [self.series(item.id) for item in series],
        }

    def create_shelf(self, body):
        row = Shelf(
            id=uid("shelf"),
            user_id=USER_ID,
            name=body.name,
            domain=body.domain,
            specialty=body.specialty,
            tags_json=dump(body.tags),
        )
        self.db.add(row)
        self.db.commit()
        return self._shelf(row)

    async def create_plan(self, body, idempotency_key: str | None = None):
        self.shelf(body.shelf_id)
        request = body.model_dump(by_alias=False)
        request_key = (idempotency_key or uid("plan_request")).strip()
        if len(request_key) < 8 or len(request_key) > 128:
            raise AppError("创建请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        reservation = self.db.get(PlanCreationRequest, request_key)
        owns_reservation = False
        if not reservation:
            reservation = PlanCreationRequest(
                idempotency_key=request_key,
                user_id=USER_ID,
                request_hash=request_hash,
                status="pending",
            )
            self.db.add(reservation)
            try:
                self.db.commit()
                owns_reservation = True
            except IntegrityError:
                self.db.rollback()
                reservation = self.db.get(PlanCreationRequest, request_key)
        if not reservation or reservation.user_id != USER_ID or reservation.request_hash != request_hash:
            raise AppError("创建请求标识已用于其他学习计划", code="IDEMPOTENCY_KEY_REUSED", status=409)
        if reservation.status == "completed" and reservation.series_id:
            return self.series(reservation.series_id)
        if reservation.status == "failed":
            reservation.status = "pending"
            reservation.error_code = ""
            reservation.updated_at = now()
            self.db.commit()
            owns_reservation = True
        elif not owns_reservation:
            raise AppError("相同学习计划正在生成，请勿重复提交", code="PLAN_CREATION_IN_PROGRESS", status=409)
        try:
            generated = await self.ai.plan(request, self._memory(body.shelf_id))
        except Exception as error:
            self.db.rollback()
            failed = self.db.get(PlanCreationRequest, request_key)
            if failed:
                failed.status = "failed"
                failed.error_code = getattr(error, "code", error.__class__.__name__)[:80]
                failed.updated_at = now()
                self.db.commit()
            raise
        plan = LearningPlan(
            id=uid("plan"),
            **request,
            assumptions_json=dump(generated.assumptions),
            confidence=generated.confidence,
            status="active",
        )
        series = Series(
            id=uid("series"),
            plan_id=plan.id,
            shelf_id=body.shelf_id,
            title=generated.series_title,
            rationale=generated.rationale,
        )
        self.db.add_all([plan, series])
        reservation.status = "completed"
        reservation.series_id = series.id
        reservation.updated_at = now()
        self.db.flush()
        for book_position, item in enumerate(generated.books, 1):
            book = Book(
                id=uid("book"),
                series_id=series.id,
                shelf_id=body.shelf_id,
                position=book_position,
                title=item.title,
                topic=item.topic,
                description=item.description,
                estimated_minutes=item.estimated_minutes,
                status="available" if book_position == 1 else "locked",
            )
            self.db.add(book)
            self.db.flush()
            self.db.add(
                BookCapstone(
                    id=uid("capstone"),
                    book_id=book.id,
                    title=f"《{book.title}》全书大作业",
                    brief_json=dump(
                        {
                            "goal": f"综合运用《{book.title}》的关键机制完成一个可复核成果",
                            "deliverables": ["方案或实现", "验证记录", "边界与复盘"],
                        }
                    ),
                    status="locked",
                )
            )
            for chapter_position, chapter in enumerate(item.chapters, 1):
                self.db.add(
                    Chapter(
                        id=uid("chapter"),
                        book_id=book.id,
                        position=chapter_position,
                        title=chapter.title,
                        objective=chapter.objective,
                        status="available" if book_position == 1 and chapter_position == 1 else "locked",
                    )
                )
        self.db.commit()
        return self.series(series.id)

    def series(self, series_id):
        series = self.db.get(Series, series_id)
        if not series or series.deleted_at is not None:
            raise AppError("系列不存在", code="SERIES_NOT_FOUND", status=404)
        books = self.db.scalars(select(Book).where(Book.series_id == series.id).order_by(Book.position)).all()
        payload = [self.book(item.id) for item in books]
        total_minutes = sum(item.estimated_minutes for item in books) or 1
        completed_minutes = sum(item.estimated_minutes * self._book_progress(item) for item in books)
        return {
            "id": series.id,
            "title": series.title,
            "rationale": series.rationale,
            "progress": round(completed_minutes / total_minutes * 100),
            "progressBasis": "bookEstimatedMinutesWithFutureChapterProjection",
            "books": payload,
        }

    def delete_series(self, series_id):
        series = self.db.scalar(
            select(Series)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Series.id == series_id,
                Series.deleted_at.is_(None),
                Shelf.user_id == USER_ID,
            )
        )
        if not series:
            raise AppError("系列不存在", code="SERIES_NOT_FOUND", status=404)
        series.deleted_at = now()
        plan = self.db.get(LearningPlan, series.plan_id)
        if plan:
            plan.status = "deleted"
        self.db.commit()

    def _book_progress(self, book):
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id)).all()
        section_units = 0.0
        for chapter in chapters:
            sections = self.db.scalars(select(Section).where(Section.chapter_id == chapter.id)).all()
            denominator = len(sections) if sections else EXPECTED_SECTIONS_PER_CHAPTER
            section_units += sum(item.status == "completed" for item in sections) / denominator
        chapter_ratio = section_units / len(chapters) if chapters else 0
        return chapter_ratio

    def _book_practice_progress(self, book):
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id)).all()
        practices = self.db.scalars(
            select(ChapterPractice).join(Chapter, Chapter.id == ChapterPractice.chapter_id).where(Chapter.book_id == book.id)
        ).all()
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book.id))
        total = len(chapters) + 1
        done = sum(item.status == "completed" for item in practices) + int(bool(capstone and capstone.status == "completed"))
        return done / total if total else 0

    def book(self, book_id):
        book = self.db.get(Book, book_id)
        if not book:
            raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book.id))
        return {
            "id": book.id,
            "seriesId": book.series_id,
            "position": book.position,
            "title": book.title,
            "description": book.description,
            "estimatedMinutes": book.estimated_minutes,
            "status": book.status,
            "progress": round(self._book_progress(book) * 100),
            "practiceProgress": round(self._book_practice_progress(book) * 100),
            "chapters": [self._chapter(item) for item in chapters],
            "capstone": self._capstone(capstone) if capstone else None,
        }

    def _chapter(self, chapter):
        sections = self.db.scalars(select(Section).where(Section.chapter_id == chapter.id).order_by(Section.position)).all()
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter.id))
        return {
            "id": chapter.id,
            "position": chapter.position,
            "title": chapter.title,
            "objective": chapter.objective,
            "status": chapter.status,
            "generated": bool(sections),
            "sections": [self._section_summary(item) for item in sections],
            "practice": self._practice(practice) if practice else None,
        }

    def _section_summary(self, section):
        return {
            "id": section.id,
            "position": section.position,
            "title": section.title,
            "question": section.question,
            "objectives": load(section.objectives_json, []),
            "status": section.status,
            "bestScore": section.best_score,
            "totalScore": section.total_score,
            "askMeUnlocked": section.ask_me_unlocked,
        }

    def _assert_future(self, chapter):
        generated = self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == chapter.id))
        if chapter.status == "completed" or generated:
            raise AppError("已开始章节不能调整", code="CHAPTER_ALREADY_STARTED", status=409)

    def add_chapter(self, book_id, body):
        book = self.db.get(Book, book_id)
        if not book:
            raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        started_end = max((item.position for item in chapters if item.status == "completed" or self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id))), default=0)
        position = body.position or len(chapters) + 1
        if position <= started_end or position > len(chapters) + 1:
            raise AppError("只能在未开始章节范围内新增", code="CHAPTER_POSITION_INVALID", status=409)
        for item in reversed(chapters):
            if item.position >= position:
                item.position += 1000
        self.db.flush()
        for item in chapters:
            if item.position >= 1000:
                item.position -= 999
        row = Chapter(id=uid("chapter"), book_id=book.id, position=position, title=body.title, objective=body.objective, status="locked")
        self.db.add(row)
        self.db.add(ChapterRevision(id=uid("revision"), book_id=book.id, action="add", after_json=dump({"id": row.id, "position": position, "title": row.title})))
        self.db.commit()
        return self._chapter(row)

    def update_chapter(self, chapter_id, body):
        chapter = self.db.get(Chapter, chapter_id)
        if not chapter:
            raise AppError("章不存在", code="CHAPTER_NOT_FOUND", status=404)
        self._assert_future(chapter)
        before = {"title": chapter.title, "objective": chapter.objective}
        if body.title is not None:
            chapter.title = body.title
        if body.objective is not None:
            chapter.objective = body.objective
        self.db.add(ChapterRevision(id=uid("revision"), book_id=chapter.book_id, action="update", before_json=dump(before), after_json=dump({"title": chapter.title, "objective": chapter.objective})))
        self.db.commit()
        return self._chapter(chapter)

    def delete_chapter(self, chapter_id):
        chapter = self.db.get(Chapter, chapter_id)
        if not chapter:
            raise AppError("章不存在", code="CHAPTER_NOT_FOUND", status=404)
        self._assert_future(chapter)
        book_id, old_position = chapter.book_id, chapter.position
        count = self.db.scalar(select(func.count()).select_from(Chapter).where(Chapter.book_id == book_id))
        if count <= 1:
            raise AppError("一本书至少保留一章", code="LAST_CHAPTER", status=409)
        self.db.add(ChapterRevision(id=uid("revision"), book_id=book_id, action="delete", before_json=dump({"id": chapter.id, "position": old_position, "title": chapter.title})))
        self.db.delete(chapter)
        self.db.flush()
        later = self.db.scalars(select(Chapter).where(Chapter.book_id == book_id, Chapter.position > old_position).order_by(Chapter.position)).all()
        for item in later:
            item.position += 1000
        self.db.flush()
        for item in later:
            item.position -= 1001
        self.db.commit()

    def reorder_chapters(self, book_id, chapter_ids):
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.position)).all()
        future = [item for item in chapters if not self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id)) and item.status != "completed"]
        if set(chapter_ids) != {item.id for item in future} or len(chapter_ids) != len(future):
            raise AppError("排序必须且只能包含全部未开始章节", code="CHAPTER_ORDER_INVALID", status=409)
        slots = sorted(item.position for item in future)
        by_id = {item.id: item for item in future}
        before = [item.id for item in sorted(future, key=lambda value: value.position)]
        for item in future:
            item.position += 1000
        self.db.flush()
        for position, chapter_id in zip(slots, chapter_ids, strict=True):
            by_id[chapter_id].position = position
        self.db.add(ChapterRevision(id=uid("revision"), book_id=book_id, action="reorder", before_json=dump(before), after_json=dump(chapter_ids)))
        self.db.commit()
        return self.book(book_id)

    async def replan_chapters(self, book_id):
        book = self.db.get(Book, book_id)
        if not book:
            raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        started, future = [], []
        for item in chapters:
            target = started if item.status == "completed" or self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id)) else future
            target.append(item)
        generated = await self.ai.replan_book(
            {
                "title": book.title,
                "topic": book.topic,
                "description": book.description,
                "started_chapters": [{"title": item.title, "objective": item.objective} for item in started],
                "future_chapters": [{"title": item.title, "objective": item.objective} for item in future],
            },
            self._memory(book.shelf_id),
        )
        before = [{"id": item.id, "title": item.title, "objective": item.objective, "position": item.position} for item in future]
        proposal = ChapterRevision(
            id=uid("revision"),
            book_id=book.id,
            action="ai_replan_proposal",
            before_json=dump(before),
            after_json=dump({"rationale": generated.rationale, "chapters": [item.model_dump() for item in generated.chapters]}),
        )
        self.db.add(proposal)
        self.db.commit()
        return {"proposalId": proposal.id, "rationale": generated.rationale, "chapters": [item.model_dump() for item in generated.chapters], "requiresConfirmation": True}

    def confirm_replan(self, book_id, proposal_id):
        book = self.db.get(Book, book_id)
        proposal = self.db.get(ChapterRevision, proposal_id)
        if not book or not proposal or proposal.book_id != book_id or proposal.action != "ai_replan_proposal":
            raise AppError("重规划提案不存在", code="REPLAN_PROPOSAL_NOT_FOUND", status=404)
        chapters = self.db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.position)).all()
        started, future = [], []
        for item in chapters:
            target = started if item.status == "completed" or self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == item.id)) else future
            target.append(item)
        current = [{"id": item.id, "title": item.title, "objective": item.objective, "position": item.position} for item in future]
        if current != load(proposal.before_json, []):
            raise AppError("未来章节已变化，请重新生成提案", code="REPLAN_PROPOSAL_STALE", status=409)
        proposed = load(proposal.after_json, {})
        for item in future:
            self.db.delete(item)
        self.db.flush()
        for offset, item in enumerate(proposed["chapters"], len(started) + 1):
            self.db.add(Chapter(id=uid("chapter"), book_id=book.id, position=offset, title=item["title"], objective=item["objective"], status="locked"))
        proposal.action = "ai_replan_confirmed"
        self.db.commit()
        return self.book(book.id)

    async def generate_chapter(self, chapter_id):
        chapter = self.db.get(Chapter, chapter_id)
        if not chapter:
            raise AppError("章不存在", code="CHAPTER_NOT_FOUND", status=404)
        if chapter.status == "locked":
            raise AppError("请先完成前置学习", code="CHAPTER_LOCKED", status=403)
        if not self.db.scalar(select(func.count()).select_from(Section).where(Section.chapter_id == chapter.id)):
            book = self.db.get(Book, chapter.book_id)
            generated = await self.ai.chapter({"title": chapter.title, "objective": chapter.objective}, self._memory(book.shelf_id))
            for position, item in enumerate(generated.sections, 1):
                self.db.add(
                    Section(
                        id=uid("section"),
                        chapter_id=chapter.id,
                        position=position,
                        title=item.title,
                        question=item.question,
                        objectives_json=dump(item.objectives),
                        status="available" if position == 1 else "locked",
                    )
                )
            self.db.add(
                ChapterPractice(
                    id=uid("practice"),
                    chapter_id=chapter.id,
                    title=f"{chapter.title}：章末实践",
                    instructions_json=dump(
                        {
                            "objective": chapter.objective,
                            "steps": ["完成一个最小实践", "保存输入、输出或截图证据", "记录失败边界与复盘"],
                        }
                    ),
                    status="locked",
                )
            )
            self.db.commit()
        return self._chapter(chapter)

    async def generate_section(self, section_id, retry=False, retry_attempt_id=None):
        section = self.db.get(Section, section_id)
        if not section:
            raise AppError("小节不存在", code="SECTION_NOT_FOUND", status=404)
        if section.status == "locked":
            raise AppError("小节未解锁", code="SECTION_LOCKED", status=403)
        existing = self.db.scalar(select(ContentVersion).where(ContentVersion.section_id == section.id).order_by(ContentVersion.version.desc()))
        latest_quiz = self.db.scalar(select(QuizSet).where(QuizSet.section_id == section.id).order_by(QuizSet.generation.desc()))
        if existing and not retry:
            return self.section(section.id)
        running = self.db.scalar(select(GenerationRun).where(GenerationRun.section_id == section.id, GenerationRun.status == "running").order_by(GenerationRun.started_at.desc()))
        if running:
            started = running.started_at if running.started_at.tzinfo else running.started_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() < 300:
                raise AppError("本节正在生成，请稍后读取状态", code="GENERATION_IN_PROGRESS", status=409)
            running.status, running.error_code, running.error_message, running.finished_at = "failed", "GENERATION_ABANDONED", "上一次生成超过 5 分钟未完成，已允许安全重试", now()
            self.db.commit()
        attempt = (self.db.scalar(select(func.max(GenerationRun.attempt)).where(GenerationRun.section_id == section.id)) or 0) + 1
        run = GenerationRun(
            id=uid("generation"),
            section_id=section.id,
            operation="remediation" if retry else "lesson",
            attempt=attempt,
            status="running",
            model=getattr(self.ai, "model", ""),
            trace_json=dump({"stage": "model_call", "retry": retry}),
        )
        self.db.add(run)
        self.db.commit()
        try:
            prior = load(latest_quiz.questions_json, []) if retry and latest_quiz else []
            book = self._book_for_section(section)
            remediation_count = self.db.scalar(select(func.count()).select_from(Remediation).where(Remediation.section_id == section.id)) if retry else 0
            remediation_strategy = ["paragraph_locator", "alternative_explanation", "prerequisite_supplement"][min(remediation_count, 2)] if retry else None
            lesson = None
            verification = []
            rejected_source_urls: list[str] = []
            max_generation_attempts = 4
            for novelty_attempt in range(1, max_generation_attempts + 1):
                memory = self._memory(book.shelf_id)
                memory_trace = {"memoryApplied": bool(memory), "memoryConceptCount": len(memory)}
                run.trace_json = dump({"stage": "model_call", "retry": retry, "noveltyAttempt": novelty_attempt, **memory_trace})
                self.db.commit()
                lesson_request = {**self._section_summary(section), "rejectedSourceUrls": rejected_source_urls}
                if retry:
                    lesson_request["remediationStrategy"] = remediation_strategy
                lesson = await self.ai.lesson(lesson_request, memory, prior)
                run.trace_json = dump({"stage": "source_verification", "retry": retry, "noveltyAttempt": novelty_attempt, "sourceUrls": [item.url for item in lesson.sources], **memory_trace})
                self.db.commit()
                try:
                    verification = await self.source_verifier.verify(lesson.sources)
                except AppError as error:
                    rejected_source_urls.extend(item.url for item in lesson.sources)
                    rejected_source_urls = list(dict.fromkeys(rejected_source_urls))
                    if error.code == "SOURCE_UNREACHABLE" and novelty_attempt < max_generation_attempts:
                        lesson = None
                        continue
                    raise
                if not retry or self._questions_are_novel(prior, [item.model_dump() for item in lesson.questions]):
                    break
                lesson = None
            if lesson is None:
                raise AppError("模型连续返回与旧题实质相同的题集", code="QUIZ_NOT_NOVEL", status=502)
            content = existing
            if not retry:
                content = ContentVersion(
                    id=uid("content"),
                    section_id=section.id,
                    version=(existing.version + 1 if existing else 1),
                    blocks_json="[]",
                    sources_json=dump([item.model_dump() for item in lesson.sources]),
                    confidence=lesson.confidence,
                )
                blocks = []
                for position, block in enumerate(lesson.blocks, 1):
                    payload = block.model_dump()
                    payload["id"] = f"block_{content.id}_{position}"
                    payload["version"] = content.version
                    blocks.append(payload)
                content.blocks_json = dump(blocks)
                self.db.add(content)
                self.db.flush()
                self.db.add(SourceVerification(id=uid("verification"), content_version_id=content.id, report_json=dump(verification)))
            quiz = QuizSet(
                id=uid("quiz"),
                section_id=section.id,
                generation=(latest_quiz.generation + 1 if latest_quiz else 1),
                questions_json=dump([item.model_dump() for item in lesson.questions]),
            )
            self.db.add(quiz)
            self.db.flush()
            if retry:
                if not retry_attempt_id:
                    raise AppError("补救教学必须绑定失败答题", code="REMEDIATION_ATTEMPT_REQUIRED")
                remediation_blocks = []
                for position, block in enumerate(lesson.blocks, 1):
                    payload = block.model_dump()
                    payload["id"] = f"block_remediation_{quiz.id}_{position}"
                    payload["version"] = quiz.generation
                    remediation_blocks.append(payload)
                failed_objectives = sorted(
                    {
                        item["objective"]
                        for item in load(self.db.get(QuizAttempt, retry_attempt_id).results_json, [])
                        if not item["correct"]
                    }
                )
                self.db.add(
                    Remediation(
                        id=uid("remediation"),
                        section_id=section.id,
                        attempt_id=retry_attempt_id,
                        replacement_quiz_id=quiz.id,
                        blocks_json=dump(remediation_blocks),
                        objectives_json=dump(failed_objectives),
                        strategy=remediation_strategy,
                    )
                )
            run.status, run.finished_at = "succeeded", now()
            run.trace_json = dump({"stage": "persisted", "contentVersionId": content.id if content else None, "quizSetId": quiz.id, "sourceVerification": verification, **memory_trace})
            self.db.commit()
            return self.section(section.id)
        except BaseException as error:
            self.db.rollback()
            run = self.db.get(GenerationRun, run.id)
            if run:
                run.status = "failed"
                run.error_code = getattr(error, "code", type(error).__name__)
                run.error_message = str(error)[:2000]
                run.finished_at = now()
                previous_trace = load(run.trace_json, {})
                run.trace_json = dump({**previous_trace, "stage": "failed"})
                self.db.commit()
            if isinstance(error, (AppError, KeyboardInterrupt, SystemExit)):
                raise
            raise AiError("小节生成失败；失败状态已保存，可安全重试") from error

    def _questions_are_novel(self, prior, current):
        if not prior or len(prior) != len(current):
            return False
        if Counter(item["objective"] for item in prior) != Counter(item["objective"] for item in current):
            return False
        prior_by_objective = {}
        for item in prior:
            prior_by_objective.setdefault(item["objective"], []).append(item)
        for question in current:
            candidates = prior_by_objective.get(question["objective"], [])
            if any(
                normalized(question["prompt"]) == normalized(old["prompt"])
                or {normalized(option) for option in question["options"]} == {normalized(option) for option in old["options"]}
                for old in candidates
            ):
                return False
            if question.get("difficulty", "standard") != "standard":
                return False
        return True

    def section(self, section_id):
        section = self.db.get(Section, section_id)
        if not section:
            raise AppError("小节不存在", code="SECTION_NOT_FOUND", status=404)
        content = self.db.scalar(select(ContentVersion).where(ContentVersion.section_id == section.id).order_by(ContentVersion.version.desc()))
        quiz = self.db.scalar(select(QuizSet).where(QuizSet.section_id == section.id).order_by(QuizSet.generation.desc()))
        note = self.db.scalar(select(LearningNote).where(LearningNote.section_id == section.id))
        run = self.db.scalar(select(GenerationRun).where(GenerationRun.section_id == section.id).order_by(GenerationRun.started_at.desc()))
        remediations = self.db.scalars(select(Remediation).where(Remediation.section_id == section.id).order_by(Remediation.created_at)).all()
        verification = self.db.scalar(select(SourceVerification).where(SourceVerification.content_version_id == content.id)) if content else None
        questions = load(quiz.questions_json, []) if quiz else []
        public = [
            {
                **{key: value for key, value in question.items() if key != "correct"},
                "selectionMode": "multiple" if len(set(question.get("correct", []))) > 1 else "single",
            }
            for question in questions
        ]
        return {
            **self._section_summary(section),
            "generation": self._generation(run) if run else None,
            "content": {
                "id": content.id,
                "version": content.version,
                "blocks": load(content.blocks_json, []),
                "sources": load(content.sources_json, []),
                "sourceVerification": load(verification.report_json, []) if verification else [],
                "confidence": content.confidence,
            }
            if content
            else None,
            "quiz": {"id": quiz.id, "generation": quiz.generation, "questions": public} if quiz else None,
            "remediations": [
                {
                    "id": item.id,
                    "attemptId": item.attempt_id,
                    "replacementQuizId": item.replacement_quiz_id,
                    "blocks": load(item.blocks_json, []),
                    "objectives": load(item.objectives_json, []),
                    "strategy": item.strategy,
                }
                for item in remediations
            ],
            "note": self._note(note) if note else None,
        }

    def _generation(self, run):
        return {
            "id": run.id,
            "operation": run.operation,
            "attempt": run.attempt,
            "status": run.status,
            "model": run.model,
            "trace": load(run.trace_json, {}),
            "errorCode": run.error_code or None,
            "error": run.error_message or None,
            "startedAt": timestamp(run.started_at),
            "finishedAt": timestamp(run.finished_at),
        }

    async def submit_quiz(self, section_id, body):
        section = self.db.get(Section, section_id)
        quiz = self.db.get(QuizSet, body.quiz_set_id)
        latest = self.db.scalar(select(QuizSet).where(QuizSet.section_id == section_id).order_by(QuizSet.generation.desc()))
        if not section or not quiz or quiz.section_id != section_id:
            raise AppError("题集无效", code="QUIZ_INVALID")
        if not latest or latest.id != quiz.id:
            raise AppError("旧题集已失效，请提交当前题集", code="QUIZ_STALE", status=409)
        questions = load(quiz.questions_json, [])
        grade = grade_choice_quiz(questions, body.answers)
        attempt = QuizAttempt(
            id=uid("attempt"),
            quiz_set_id=quiz.id,
            user_id=USER_ID,
            answers_json=dump(body.answers),
            results_json=dump(grade.results),
            passed=grade.passed,
        )
        self.db.add(attempt)
        section.best_score, section.total_score = max(section.best_score, grade.score), grade.total
        section.ask_me_unlocked |= grade.perfect
        section.status = "completed" if grade.passed else "available"
        self.db.flush()
        self._record_quiz_evidence(section, questions, grade, attempt.id)
        self.db.commit()
        if grade.passed:
            await self._ensure_note(section)
            self._unlock_next(section)
            self.db.commit()
        else:
            await self.generate_section(section_id, retry=True, retry_attempt_id=attempt.id)
        return {
            "attemptId": attempt.id,
            "score": grade.score,
            "total": grade.total,
            "passed": grade.passed,
            "perfect": grade.perfect,
            "results": grade.results,
            "remediation": self.section(section_id)["remediations"][-1] if not grade.passed else None,
            "nextQuiz": self.section(section_id)["quiz"] if not grade.passed else None,
        }

    def _record_quiz_evidence(self, section, questions, grade, attempt_id):
        context = self._context(section)
        for question, result in zip(questions, grade.results, strict=True):
            delta = 18 if result["correct"] else -12
            self._add_evidence(context, question["objective"], "quiz", {"attemptId": attempt_id, "correct": result["correct"], "core": question.get("core", False)}, delta)

    def _add_evidence(self, context, concept, evidence_type, result, delta):
        evidence = LearningEvidence(
            id=uid("evidence"),
            user_id=USER_ID,
            shelf_id=context["shelf"].id,
            series_id=context["series"].id,
            book_id=context["book"].id,
            chapter_id=context["chapter"].id,
            section_id=context["section"].id,
            concept=concept[:300],
            evidence_type=evidence_type,
            result_json=dump(result),
            mastery_delta=delta,
        )
        self.db.add(evidence)
        memory = self.db.scalar(select(LearningMemory).where(LearningMemory.user_id == USER_ID, LearningMemory.shelf_id == context["shelf"].id, LearningMemory.concept == concept[:300]))
        if not memory:
            memory = LearningMemory(id=uid("memory"), user_id=USER_ID, shelf_id=context["shelf"].id, concept=concept[:300], mastery_score=0, evidence_count=0, summary="")
            self.db.add(memory)
        memory.mastery_score = max(0, min(100, memory.mastery_score + delta))
        memory.evidence_count += 1
        memory.summary = f"{memory.evidence_count} 条证据，当前掌握度 {memory.mastery_score}/100；最近证据：{evidence_type}"
        memory.updated_at = now()

    def _memory(self, shelf_id, limit=30):
        rows = self.db.scalars(select(LearningMemory).where(LearningMemory.user_id == USER_ID, LearningMemory.shelf_id == shelf_id).order_by(LearningMemory.updated_at.desc()).limit(limit)).all()
        return [{"concept": item.concept, "mastery": item.mastery_score, "evidenceCount": item.evidence_count, "summary": item.summary} for item in rows]

    def learning_memory(self, shelf_id=None):
        if shelf_id:
            self.shelf(shelf_id)
            return self._memory(shelf_id, 200)
        shelves = self.db.scalars(select(Shelf).where(Shelf.user_id == USER_ID)).all()
        return {item.id: self._memory(item.id, 200) for item in shelves}

    def _unlock_next(self, section):
        chapter = self.db.get(Chapter, section.chapter_id)
        next_section = self.db.scalar(select(Section).where(Section.chapter_id == chapter.id, Section.position > section.position).order_by(Section.position))
        if next_section:
            next_section.status = "available"
            return
        chapter.status = "completed"
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter.id))
        if practice:
            practice.status = "available"
        book = self.db.get(Book, chapter.book_id)
        next_chapter = self.db.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.position > chapter.position).order_by(Chapter.position))
        if next_chapter:
            next_chapter.status = "available"
            return
        book.status = "completed"
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book.id))
        if capstone:
            capstone.status = "available"
        next_book = self.db.scalar(select(Book).where(Book.series_id == book.series_id, Book.position > book.position).order_by(Book.position))
        if next_book:
            next_book.status = "available"
            first = self.db.scalar(select(Chapter).where(Chapter.book_id == next_book.id).order_by(Chapter.position))
            if first:
                first.status = "available"

    async def _ensure_note(self, section):
        if self.db.scalar(select(LearningNote).where(LearningNote.section_id == section.id)):
            return
        view = self.section(section.id)
        messages = self.db.scalars(select(QaMessage).join(QaSession, QaSession.id == QaMessage.session_id).where(QaSession.section_id == section.id).order_by(QaMessage.created_at)).all()
        attempts = self.db.scalars(select(QuizAttempt).join(QuizSet, QuizSet.id == QuizAttempt.quiz_set_id).where(QuizSet.section_id == section.id).order_by(QuizAttempt.created_at)).all()
        generated = await self.ai.note(
            {
                "section": view,
                "qa": [{"role": item.role, "content": item.content, "threadId": item.thread_id} for item in messages],
                "quizEvidence": [load(item.results_json, []) for item in attempts],
            }
        )
        self.db.add(LearningNote(id=uid("note"), section_id=section.id, user_id=USER_ID, ai_content_json=dump(generated.model_dump()), user_content_json="{}"))

    def _note(self, note):
        return {"id": note.id, "aiContent": load(note.ai_content_json, {}), "userContent": load(note.user_content_json, {}), "version": note.version}

    def update_note(self, section_id, content):
        note = self.db.scalar(select(LearningNote).where(LearningNote.section_id == section_id, LearningNote.user_id == USER_ID))
        if not note:
            raise AppError("笔记不存在", code="NOTE_NOT_FOUND", status=404)
        note.user_content_json, note.version, note.updated_at = dump(content), note.version + 1, now()
        self.db.commit()
        return self._note(note)

    async def ask(self, section_id, body):
        section_view = self.section(section_id)
        if not section_view["content"]:
            raise AppError("请先生成本节", code="SECTION_NOT_GENERATED")
        valid_blocks = {item["id"] for item in section_view["content"]["blocks"]}
        if body.block_id not in valid_blocks:
            raise AppError("内容块不存在或版本已失效", code="BLOCK_INVALID", status=409)
        section_row = self.db.get(Section, section_id)
        session = self.db.scalar(select(QaSession).where(QaSession.section_id == section_id, QaSession.user_id == USER_ID))
        if not session:
            session = QaSession(id=uid("qa"), section_id=section_id, user_id=USER_ID)
            self.db.add(session)
            self.db.flush()
        messages = self.db.scalars(select(QaMessage).where(QaMessage.session_id == session.id).order_by(QaMessage.created_at)).all()
        threads = self.db.scalars(select(QaThread).where(QaThread.session_id == session.id).order_by(QaThread.updated_at.desc())).all()
        current_history = [
            {"role": item.role, "content": item.content, "blockId": item.block_id}
            for item in messages
            if body.thread_id and item.thread_id == body.thread_id
        ]
        related_summaries = [
            {"threadId": item.thread_id, "summary": item.summary}
            for item in threads
            if item.thread_id != body.thread_id and item.summary
        ][:5]
        suggested = uid("thread")
        result = await self.ai.answer(
            {
                "section": section_view,
                "anchorBlockId": body.block_id,
                "question": body.question,
                "requestedThreadId": body.thread_id,
                "forcedRelation": body.force_relation,
                "newThreadId": suggested,
                "weightedContext": {
                    "currentThreadFullHistory": current_history,
                    "relatedThreadSummaries": related_summaries,
                    "crossSectionMemory": self._memory(self._book_for_section(section_row).shelf_id, 10),
                },
            }
        )
        relation = body.force_relation or result.relation
        if relation == "follow_up" and body.thread_id:
            thread_id = body.thread_id
        else:
            relation, thread_id = "new_question", suggested
        thread = self.db.scalar(select(QaThread).where(QaThread.session_id == session.id, QaThread.thread_id == thread_id))
        if not thread:
            thread = QaThread(id=uid("qathread"), session_id=session.id, thread_id=thread_id, classification=relation)
            self.db.add(thread)
        thread_summary = result.thread_summary.strip() or result.answer.strip()[:240]
        thread.summary, thread.updated_at = thread_summary, now()
        self.db.add_all(
            [
                QaMessage(id=uid("msg"), session_id=session.id, thread_id=thread_id, block_id=body.block_id, role="user", content=body.question),
                QaMessage(id=uid("msg"), session_id=session.id, thread_id=thread_id, block_id=body.block_id, role="assistant", content=result.answer),
            ]
        )
        memory = load(session.memory_json, {"threads": {}}) or {"threads": {}}
        memory.setdefault("threads", {})[thread_id] = thread_summary
        memory["lastThread"] = thread_id
        session.memory_json = dump(memory)
        self.db.commit()
        return {"sessionId": session.id, "threadId": thread_id, "relation": relation, "answer": result.answer, "classificationCorrectable": True}

    def correct_qa_classification(self, section_id, thread_id, body):
        session = self.db.scalar(select(QaSession).where(QaSession.section_id == section_id, QaSession.user_id == USER_ID))
        thread = self.db.scalar(select(QaThread).where(QaThread.session_id == session.id, QaThread.thread_id == thread_id)) if session else None
        if not thread:
            raise AppError("答疑线程不存在", code="QA_THREAD_NOT_FOUND", status=404)
        if body.relation == "follow_up":
            target = self.db.scalar(select(QaThread).where(QaThread.session_id == session.id, QaThread.thread_id == body.target_thread_id)) if body.target_thread_id else None
            if not target or target.thread_id == thread_id:
                raise AppError("纠正为追问时必须指定另一条已有线程", code="QA_TARGET_INVALID")
            messages = self.db.scalars(select(QaMessage).where(QaMessage.session_id == session.id, QaMessage.thread_id == thread_id)).all()
            for item in messages:
                item.thread_id = target.thread_id
            target.summary = "；".join(value for value in [target.summary, thread.summary] if value)
            target.corrected, target.updated_at = True, now()
            self.db.delete(thread)
            corrected_id = target.thread_id
        else:
            thread.classification, thread.corrected, thread.updated_at = "new_question", True, now()
            corrected_id = thread.thread_id
        self.db.commit()
        return {"threadId": corrected_id, "relation": body.relation, "corrected": True}

    async def ask_me(self, section_id, answer):
        section = self.db.get(Section, section_id)
        if not section:
            raise AppError("小节不存在", code="SECTION_NOT_FOUND", status=404)
        if not section.ask_me_unlocked:
            raise AppError("小节满分后才解锁 Ask Me", code="ASK_ME_LOCKED", status=403)
        session = self.db.scalar(select(AskMeSession).where(AskMeSession.section_id == section.id, AskMeSession.user_id == USER_ID))
        entries = load(session.entries_json, []) if session else []
        dimensions = ["mechanism", "boundary", "transfer"]
        if session and session.status == "completed":
            return self._ask_me(session)
        if not session:
            if answer:
                raise AppError("请先开始 Ask Me 再作答", code="ASK_ME_NOT_STARTED")
            turn = None
            for validation_attempt in range(1, 4):
                turn = await self.ai.ask_me({"section": self.section(section_id), "dimension": "mechanism", "previousAnswer": None, "finalize": False, "validationAttempt": validation_attempt, "requiredEvaluation": "not_evaluated"})
                if turn.dimension == "mechanism" and turn.evaluation == "not_evaluated":
                    break
            if turn is None or turn.dimension != "mechanism" or turn.evaluation != "not_evaluated":
                raise AiError("Ask Me 首轮结构无效")
            session = AskMeSession(id=uid("askme"), section_id=section.id, user_id=USER_ID, round_index=0, entries_json=dump([{"dimension": "mechanism", "prompt": turn.prompt, "answer": None, "evaluation": "not_evaluated", "rationale": ""}]))
            self.db.add(session)
            self.db.commit()
            return self._ask_me(session)
        if not answer:
            raise AppError("本轮回答不能为空", code="ASK_ME_ANSWER_REQUIRED")
        current = session.round_index
        current_dimension = dimensions[current]
        finalize = current == 2
        requested_dimension = current_dimension if finalize else dimensions[current + 1]
        turn = None
        for validation_attempt in range(1, 4):
            turn = await self.ai.ask_me(
                {
                    "section": self.section(section_id),
                    "dimension": requested_dimension,
                    "evaluatesDimension": current_dimension,
                    "previousPrompt": entries[current]["prompt"],
                    "previousAnswer": answer,
                    "priorRounds": entries,
                    "finalize": finalize,
                    "validationAttempt": validation_attempt,
                    "requiredEvaluation": ["strong", "partial", "weak"],
                }
            )
            if turn.dimension == requested_dimension and turn.evaluation != "not_evaluated":
                break
        if turn is None or turn.evaluation == "not_evaluated":
            raise AiError("Ask Me 作答后必须给出能力评估")
        entries[current].update({"answer": answer, "evaluation": turn.evaluation, "rationale": turn.rationale})
        delta = {"strong": 20, "partial": 8, "weak": -5}[turn.evaluation]
        self._add_evidence(self._context(section), f"{section.title}:{current_dimension}", "ask_me", {"dimension": current_dimension, "evaluation": turn.evaluation}, delta)
        if finalize:
            session.status = "completed"
        else:
            if turn.dimension != requested_dimension:
                raise AiError("Ask Me 轮次顺序无效")
            entries.append({"dimension": requested_dimension, "prompt": turn.prompt, "answer": None, "evaluation": "not_evaluated", "rationale": ""})
            session.round_index += 1
        session.entries_json, session.updated_at = dump(entries), now()
        self.db.commit()
        return self._ask_me(session)

    def _ask_me(self, session):
        entries = load(session.entries_json, [])
        return {
            "id": session.id,
            "status": session.status,
            "round": session.round_index + 1,
            "dimension": entries[session.round_index]["dimension"] if entries else "mechanism",
            "prompt": entries[session.round_index]["prompt"] if session.status != "completed" and entries else None,
            "entries": entries,
        }

    def chapter_practice(self, chapter_id):
        chapter = self.db.get(Chapter, chapter_id)
        if not chapter:
            raise AppError("章不存在", code="CHAPTER_NOT_FOUND", status=404)
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter_id))
        if not practice:
            raise AppError("请先生成本章", code="PRACTICE_NOT_GENERATED", status=404)
        return self._practice(practice)

    def upload_chapter_practice_attachment(self, chapter_id, filename, media_type, data):
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter_id))
        if not practice:
            raise AppError("章末实践不存在", code="PRACTICE_NOT_FOUND", status=404)
        if practice.status == "locked":
            raise AppError("完成本章后才可上传附件", code="PRACTICE_LOCKED", status=403)
        return self._upload_attachment("chapter_practice", practice.id, filename, media_type, data)

    def submit_chapter_practice(self, chapter_id, content, attachment_ids):
        practice = self.db.scalar(select(ChapterPractice).where(ChapterPractice.chapter_id == chapter_id))
        if not practice:
            raise AppError("章末实践不存在", code="PRACTICE_NOT_FOUND", status=404)
        if practice.status == "locked":
            raise AppError("完成本章后才可提交实践", code="PRACTICE_LOCKED", status=403)
        if not content:
            raise AppError("实践提交不能为空", code="PRACTICE_EMPTY")
        attachments = self._validated_attachments("chapter_practice", practice.id, attachment_ids)
        practice.submission_json = dump({"content": content, "attachmentIds": [item.id for item in attachments]})
        practice.status, practice.updated_at = "completed", now()
        self.db.commit()
        return self._practice(practice)

    def _practice(self, practice):
        attachments = self._attachments("chapter_practice", practice.id)
        return {"id": practice.id, "title": practice.title, "instructions": load(practice.instructions_json, {}), "submission": load(practice.submission_json, {}), "attachments": attachments, "evidenceMode": "file_attachment" if attachments else "structured_only_legacy", "status": practice.status}

    def book_capstone(self, book_id):
        book = self.db.get(Book, book_id)
        if not book:
            raise AppError("书不存在", code="BOOK_NOT_FOUND", status=404)
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book_id))
        if not capstone:
            raise AppError("全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404)
        return self._capstone(capstone)

    def upload_book_capstone_attachment(self, book_id, filename, media_type, data):
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book_id))
        if not capstone:
            raise AppError("全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404)
        if capstone.status == "locked":
            raise AppError("完成本书正文后才可上传附件", code="CAPSTONE_LOCKED", status=403)
        return self._upload_attachment("book_capstone", capstone.id, filename, media_type, data)

    def submit_book_capstone(self, book_id, content, attachment_ids):
        capstone = self.db.scalar(select(BookCapstone).where(BookCapstone.book_id == book_id))
        if not capstone:
            raise AppError("全书大作业不存在", code="CAPSTONE_NOT_FOUND", status=404)
        if capstone.status == "locked":
            raise AppError("完成本书后才可提交大作业", code="CAPSTONE_LOCKED", status=403)
        if not content:
            raise AppError("大作业提交不能为空", code="CAPSTONE_EMPTY")
        attachments = self._validated_attachments("book_capstone", capstone.id, attachment_ids)
        capstone.submission_json = dump({"content": content, "attachmentIds": [item.id for item in attachments]})
        capstone.status, capstone.updated_at = "completed", now()
        self.db.commit()
        return self._capstone(capstone)

    def _capstone(self, capstone):
        attachments = self._attachments("book_capstone", capstone.id)
        return {"id": capstone.id, "title": capstone.title, "brief": load(capstone.brief_json, {}), "submission": load(capstone.submission_json, {}), "attachments": attachments, "evidenceMode": "file_attachment" if attachments else "structured_only_legacy", "status": capstone.status}

    def _upload_attachment(self, target_type, target_id, filename, media_type, data):
        if not self.attachment_storage:
            raise AppError("附件存储未配置", code="ATTACHMENT_STORAGE_UNAVAILABLE", status=503)
        clean_name = unquote(filename or "attachment.bin").replace("\\", "/").split("/")[-1].strip()
        if not clean_name or len(clean_name) > 255:
            raise AppError("附件文件名无效", code="ATTACHMENT_FILENAME_INVALID")
        attachment_id = uid("attachment")
        stored = self.attachment_storage.store(user_id=USER_ID, target_type=target_type, target_id=target_id, attachment_id=attachment_id, data=data)
        attachment = ArtifactAttachment(
            id=attachment_id,
            user_id=USER_ID,
            target_type=target_type,
            target_id=target_id,
            original_filename=clean_name,
            media_type=(media_type or "application/octet-stream")[:160],
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            object_key=stored.object_key,
        )
        self.db.add(attachment)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.attachment_storage.resolve(stored.object_key).unlink(missing_ok=True)
            raise
        return self._attachment(attachment)

    def _validated_attachments(self, target_type, target_id, attachment_ids):
        if not attachment_ids:
            raise AppError("必须提交至少一个真实附件", code="ATTACHMENT_REQUIRED")
        if len(set(attachment_ids)) != len(attachment_ids):
            raise AppError("附件 ID 不得重复", code="ATTACHMENT_DUPLICATE")
        attachments = self.db.scalars(
            select(ArtifactAttachment).where(
                ArtifactAttachment.id.in_(attachment_ids),
                ArtifactAttachment.user_id == USER_ID,
                ArtifactAttachment.target_type == target_type,
                ArtifactAttachment.target_id == target_id,
            )
        ).all()
        if len(attachments) != len(attachment_ids):
            raise AppError("附件不存在、无权访问或不属于当前成果", code="ATTACHMENT_INVALID", status=403)
        by_id = {item.id: item for item in attachments}
        return [by_id[item_id] for item_id in attachment_ids]

    def _attachments(self, target_type, target_id):
        items = self.db.scalars(select(ArtifactAttachment).where(ArtifactAttachment.user_id == USER_ID, ArtifactAttachment.target_type == target_type, ArtifactAttachment.target_id == target_id).order_by(ArtifactAttachment.created_at)).all()
        return [self._attachment(item) for item in items]

    def attachment(self, attachment_id):
        item = self.db.scalar(select(ArtifactAttachment).where(ArtifactAttachment.id == attachment_id, ArtifactAttachment.user_id == USER_ID))
        if not item:
            raise AppError("附件不存在", code="ATTACHMENT_NOT_FOUND", status=404)
        if not self.attachment_storage:
            raise AppError("附件存储未配置", code="ATTACHMENT_STORAGE_UNAVAILABLE", status=503)
        path = self.attachment_storage.resolve(item.object_key)
        if not path.is_file():
            raise AppError("附件对象缺失", code="ATTACHMENT_OBJECT_MISSING", status=410)
        return item, path

    @staticmethod
    def _attachment(item):
        return {"id": item.id, "filename": item.original_filename, "mediaType": item.media_type, "byteSize": item.byte_size, "sha256": item.sha256, "createdAt": timestamp(item.created_at)}

    def _book_for_section(self, section):
        chapter = self.db.get(Chapter, section.chapter_id)
        return self.db.get(Book, chapter.book_id)

    def _context(self, section):
        chapter = self.db.get(Chapter, section.chapter_id)
        book = self.db.get(Book, chapter.book_id)
        series = self.db.get(Series, book.series_id)
        shelf = self.db.get(Shelf, book.shelf_id)
        return {"section": section, "chapter": chapter, "book": book, "series": series, "shelf": shelf}
