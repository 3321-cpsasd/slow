import json
import math
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    Book,
    Chapter,
    MilestonePath,
    MilestonePathRevision,
    Series,
    Shelf,
    UserProfile,
    now,
)


RULESET_VERSION = "milestone_v1"
EXPECTED_SECTIONS_PER_CHAPTER = 4


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class MilestoneService:
    """Own milestone definitions; learning facts remain the progress authority."""

    def __init__(self, db: Session, *, user_id: str, uid):
        self.db = db
        self.user_id = user_id
        self.uid = uid

    def create_for_plan(self, *, series_id: str, generated, chapter_map: dict) -> None:
        profile = self.db.get(UserProfile, self.user_id)
        definition = self._definition_from_generated(generated, chapter_map)
        source = "ai_generation" if generated.milestones else "rule_fallback"
        path = MilestonePath(
            id=self.uid("milestone_path"),
            user_id=self.user_id,
            series_id=series_id,
            goal_profile_version=profile.version if profile else 0,
            version=1,
            status="proposed",
            definition_json=_dump(definition),
            ruleset_version=RULESET_VERSION,
        )
        self.db.add(path)
        self.db.flush()
        self._add_revision(path, source=source)

    def confirm(self, series_id: str) -> dict:
        series = self.db.scalar(
            select(Series)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Series.id == series_id,
                Series.deleted_at.is_(None),
                Shelf.user_id == self.user_id,
                Shelf.deleted_at.is_(None),
            )
        )
        if not series:
            raise AppError("学习路径不存在", code="MILESTONE_PATH_NOT_FOUND", status=404)
        path = self.db.scalar(
            select(MilestonePath).where(
                MilestonePath.series_id == series_id,
                MilestonePath.user_id == self.user_id,
            )
        )
        if not path:
            definition = self._fallback_definition(self._chapter_map(series_id))
            path = MilestonePath(
                id=self.uid("milestone_path"),
                user_id=self.user_id,
                series_id=series_id,
                goal_profile_version=0,
                version=0,
                status="proposed",
                definition_json=_dump(definition),
                ruleset_version=RULESET_VERSION,
            )
            self.db.add(path)
            self.db.flush()
        profile = self.db.get(UserProfile, self.user_id)
        goal_profile_version = profile.version if profile else 0
        if path.status == "confirmed" and path.goal_profile_version == goal_profile_version:
            return {
                "seriesId": series_id,
                "status": path.status,
                "version": path.version,
                "goalProfileVersion": path.goal_profile_version,
            }
        path.version += 1
        path.status = "confirmed"
        path.goal_profile_version = goal_profile_version
        path.confirmed_at = now()
        path.updated_at = now()
        self._add_revision(path, source="user_confirmation")
        self.db.commit()
        return {
            "seriesId": series_id,
            "status": path.status,
            "version": path.version,
            "goalProfileVersion": path.goal_profile_version,
        }

    def rebind_book_chapters(
        self,
        *,
        series_id: str,
        book_id: str,
        chapter_id_map: dict[str, str],
        replaced_chapter_ids: set[str],
        objective_by_chapter_id: dict[str, str],
    ) -> None:
        """Version milestone references after a confirmed outline replacement."""

        if not replaced_chapter_ids:
            return
        path = self.db.scalar(
            select(MilestonePath).where(
                MilestonePath.series_id == series_id,
                MilestonePath.user_id == self.user_id,
            )
        )
        if not path:
            return
        definition = _load(path.definition_json, {"milestones": []})
        changed = False
        requires_rebuild = False
        for milestone in definition.get("milestones", []):
            for criterion in milestone.get("criteria", []):
                old_chapter_id = criterion.get("chapterId")
                if (
                    criterion.get("bookId") == book_id
                    and old_chapter_id in replaced_chapter_ids
                    and old_chapter_id not in chapter_id_map
                ):
                    requires_rebuild = True
                    continue
                new_chapter_id = chapter_id_map.get(old_chapter_id)
                if criterion.get("bookId") != book_id or not new_chapter_id:
                    continue
                criterion["chapterId"] = new_chapter_id
                criterion["statement"] = objective_by_chapter_id.get(
                    new_chapter_id,
                    criterion.get("statement", ""),
                )
                changed = True
        if requires_rebuild:
            definition = self._fallback_definition(self._chapter_map(series_id))
            path.status = "proposed"
            path.confirmed_at = None
            changed = True
        if not changed:
            return
        path.definition_json = _dump(definition)
        path.version += 1
        path.updated_at = now()
        self._add_revision(
            path,
            source=(
                "book_outline_milestone_replan"
                if requires_rebuild
                else "book_outline_confirmation"
            ),
        )

    def dashboard(self, *, library: dict, profile: dict, resume: dict | None) -> dict:
        selected = self._select_series(library, resume)
        goal = {
            "statement": profile.get("purpose", ""),
            "domains": profile.get("domains", []),
            "weeklyMinutes": profile.get("weeklyMinutes", 0),
            "targetDate": profile.get("targetDate", ""),
            "profileVersion": profile.get("version", 0),
        }
        if not selected:
            return {"goal": goal, "path": None, "today": None}

        path = self.db.scalar(
            select(MilestonePath).where(
                MilestonePath.series_id == selected["id"],
                MilestonePath.user_id == self.user_id,
            )
        )
        definition = (
            _load(path.definition_json, {"milestones": []})
            if path
            else self._fallback_definition(self._view_chapter_map(selected))
        )
        chapter_views = {
            chapter["id"]: (book, chapter)
            for book in selected["books"]
            for chapter in book["chapters"]
        }
        milestone_views = []
        all_criteria = 0
        completed_criteria = 0
        for milestone in definition.get("milestones", []):
            criteria = []
            for criterion in milestone.get("criteria", []):
                book, chapter = chapter_views.get(
                    criterion.get("chapterId"),
                    ({"title": ""}, {"sections": []}),
                )
                sections = chapter.get("sections", [])
                completed_sections = sum(
                    section.get("status") == "completed" for section in sections
                )
                expected = len(sections) or EXPECTED_SECTIONS_PER_CHAPTER
                complete = bool(sections) and completed_sections == len(sections)
                all_criteria += 1
                completed_criteria += int(complete)
                criteria.append({
                    **criterion,
                    "bookTitle": book.get("title", ""),
                    "completed": complete,
                    "evidenceCount": completed_sections,
                    "expectedEvidenceCount": expected,
                })
            achieved = bool(criteria) and all(item["completed"] for item in criteria)
            milestone_views.append({**milestone, "criteria": criteria, "achieved": achieved})

        current_index = next(
            (index for index, item in enumerate(milestone_views) if not item["achieved"]),
            max(0, len(milestone_views) - 1),
        )
        current = milestone_views[current_index] if milestone_views else None
        today = self._today(selected, resume, current)
        return {
            "goal": goal,
            "path": {
                "id": path.id if path else None,
                "seriesId": selected["id"],
                "seriesTitle": selected["title"],
                "status": path.status if path else "proposed",
                "version": path.version if path else 0,
                "rulesetVersion": path.ruleset_version if path else RULESET_VERSION,
                "goalAligned": bool(
                    path
                    and path.goal_profile_version == profile.get("version", 0)
                ),
                "currentIndex": current_index,
                "progress": round(completed_criteria / all_criteria * 100) if all_criteria else 0,
                "completedCriteria": completed_criteria,
                "totalCriteria": all_criteria,
                "milestones": milestone_views,
            },
            "today": today,
        }

    def _today(self, series: dict, resume: dict | None, milestone: dict | None) -> dict | None:
        sections = [
            (book, chapter, section)
            for book in series["books"]
            for chapter in book["chapters"]
            for section in chapter["sections"]
        ]
        target = next(
            (
                item for item in sections
                if resume and item[2]["id"] == resume.get("sectionId")
                and item[2]["status"] != "locked"
            ),
            None,
        )
        if not target:
            target = next(
                (item for item in sections if item[2]["status"] not in {"locked", "completed"}),
                None,
            )
        if not target:
            target = next((item for item in sections if item[2]["status"] != "locked"), None)

        criterion = None
        if milestone:
            criterion = next(
                (item for item in milestone["criteria"] if not item["completed"]),
                milestone["criteria"][0] if milestone["criteria"] else None,
            )
        if target:
            book, chapter, section = target
            matched = next(
                (
                    item for item in (milestone or {}).get("criteria", [])
                    if item.get("chapterId") == chapter["id"]
                ),
                None,
            )
            section_objectives = section.get("objectives") or []
            section_objective = section_objectives[0] if section_objectives else None
            if isinstance(section_objective, dict):
                section_objective = section_objective.get("statement")
            if not isinstance(section_objective, str):
                section_objective = ""
            reason = (
                section_objective.strip()
                if section_objective.strip()
                else matched.get("statement")
                if matched
                else chapter.get("objective")
                or "推进当前里程碑"
            )
            return {
                "seriesId": series["id"],
                "sectionId": section["id"],
                "bookTitle": book["title"],
                "chapterTitle": chapter["title"],
                "sectionTitle": section["title"],
                "question": section["question"],
                "estimatedMinutes": 20,
                "reason": reason,
            }
        return {
            "seriesId": series["id"],
            "sectionId": None,
            "bookTitle": "",
            "chapterTitle": "",
            "sectionTitle": "准备当前里程碑的第一节",
            "question": "进入学习空间后生成并开始第一节。",
            "estimatedMinutes": 20,
            "reason": criterion.get("statement", "推进当前里程碑") if criterion else "推进当前里程碑",
        }

    @staticmethod
    def _select_series(library: dict, resume: dict | None) -> dict | None:
        series_items = [
            series
            for shelf in library.get("shelves", [])
            for series in shelf.get("series", [])
        ]
        if resume:
            for series in series_items:
                if any(
                    section["id"] == resume.get("sectionId")
                    for book in series["books"]
                    for chapter in book["chapters"]
                    for section in chapter["sections"]
                ):
                    return series
        return next((item for item in series_items if item.get("progress", 0) < 100), None) or (
            series_items[0] if series_items else None
        )

    def _definition_from_generated(self, generated, chapter_map: dict) -> dict:
        if not generated.milestones:
            return self._fallback_definition(chapter_map)
        milestones = []
        for index, milestone in enumerate(generated.milestones, 1):
            criteria = []
            for criterion_index, criterion in enumerate(milestone.criteria, 1):
                chapter, book = chapter_map[
                    (criterion.book_position, criterion.chapter_position)
                ]
                criteria.append({
                    "key": f"m{index}_c{criterion_index}",
                    "statement": criterion.statement,
                    "chapterId": chapter.id,
                    "bookId": book.id,
                    "evidenceRule": "all_section_quizzes_passed",
                })
            milestones.append({
                "key": f"m{index}",
                "title": milestone.title,
                "outcome": milestone.outcome,
                "criteria": criteria,
            })
        return {"milestones": milestones}

    def _fallback_definition(self, chapter_map: dict) -> dict:
        ordered = [chapter_map[key] for key in sorted(chapter_map)]
        if not ordered:
            return {"milestones": []}
        count = min(4, len(ordered))
        chunk_size = math.ceil(len(ordered) / count)
        chunks = [ordered[index:index + chunk_size] for index in range(0, len(ordered), chunk_size)]
        phase_labels = ["建立核心理解", "解释机制与取舍", "判断边界与异常", "完成综合迁移"]
        milestones = []
        for index, chunk in enumerate(chunks, 1):
            criteria = []
            for criterion_index, (chapter, book) in enumerate(chunk, 1):
                criteria.append({
                    "key": f"m{index}_c{criterion_index}",
                    "statement": chapter.objective if hasattr(chapter, "objective") else chapter["objective"],
                    "chapterId": chapter.id if hasattr(chapter, "id") else chapter["id"],
                    "bookId": book.id if hasattr(book, "id") else book["id"],
                    "evidenceRule": "all_section_quizzes_passed",
                })
            milestones.append({
                "key": f"m{index}",
                "title": phase_labels[min(index - 1, len(phase_labels) - 1)],
                "outcome": "；".join(item["statement"] for item in criteria),
                "criteria": criteria,
            })
        return {"milestones": milestones}

    def _chapter_map(self, series_id: str) -> dict:
        rows = self.db.execute(
            select(Book, Chapter)
            .join(Chapter, Chapter.book_id == Book.id)
            .where(Book.series_id == series_id, Book.deleted_at.is_(None))
            .order_by(Book.position, Chapter.position)
        ).all()
        return {(book.position, chapter.position): (chapter, book) for book, chapter in rows}

    @staticmethod
    def _view_chapter_map(series: dict) -> dict:
        return {
            (book["position"], chapter["position"]): (chapter, book)
            for book in series["books"]
            for chapter in book["chapters"]
        }

    def _add_revision(self, path: MilestonePath, *, source: str) -> None:
        self.db.add(
            MilestonePathRevision(
                id=self.uid("milestone_revision"),
                path_id=path.id,
                version=path.version,
                snapshot_json=_dump({
                    "status": path.status,
                    "goalProfileVersion": path.goal_profile_version,
                    "rulesetVersion": path.ruleset_version,
                    "definition": _load(path.definition_json, {}),
                }),
                source=source,
            )
        )
