import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    LearningMissionVersion,
    LearningPlan,
    LearningRun,
    MissionAdoptionEvent,
    MissionSuccessCriterion,
    MissionSuccessCriterionVersion,
    Series,
    Shelf,
    now,
)


SCHEMA_VERSION = "mission_v1"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(_dump(payload).encode()).hexdigest()


class MissionService:
    """The only writer for immutable mission versions and adoption facts."""

    def __init__(self, db: Session, *, user_id: str, uid):
        self.db = db
        self.user_id = user_id
        self.uid = uid

    def create_for_plan(self, *, plan: LearningPlan, generated) -> LearningMissionVersion:
        inferred_fields = []
        why = plan.purpose.strip()
        if not why:
            why = f"掌握{plan.topic}，并能够在{plan.role}相关场景中解释和应用关键机制"
            inferred_fields.append("why")

        capabilities = [
            {
                "bookPosition": position,
                "title": book.title,
                "topic": book.topic,
                "outcome": book.description,
            }
            for position, book in enumerate(generated.books, 1)
        ]
        payload = {
            "why": why,
            "targetCapabilities": capabilities,
            "constraints": {
                "depth": plan.depth,
                "details": plan.details,
            },
            "outOfScope": [],
            "assumptions": list(generated.assumptions),
            "learnerContext": {
                "role": plan.role,
                "experience": plan.experience,
            },
            "inferredFields": inferred_fields,
            "schemaVersion": SCHEMA_VERSION,
        }
        mission = LearningMissionVersion(
            id=self.uid("mission_version"),
            plan_id=plan.id,
            user_id=self.user_id,
            version=1,
            status="confirmed",
            why=why,
            target_capabilities_json=_dump(capabilities),
            constraints_json=_dump(payload["constraints"]),
            out_of_scope_json="[]",
            assumptions_json=_dump(payload["assumptions"]),
            learner_context_json=_dump(payload["learnerContext"]),
            inferred_fields_json=_dump(inferred_fields),
            provenance_json=_dump({
                "mode": "plan_creation",
                "sourcePlanId": plan.id,
                "purposeSource": "user_input" if not inferred_fields else "system_default",
            }),
            schema_version=SCHEMA_VERSION,
            payload_hash=_payload_hash(payload),
            confirmed_at=now(),
        )
        self.db.add(mission)
        self.db.flush()

        for position, book in enumerate(generated.books, 1):
            criterion = MissionSuccessCriterion(
                id=self.uid("mission_criterion"),
                plan_id=plan.id,
                stable_key=f"book:{position}:capstone",
            )
            self.db.add(criterion)
            self.db.flush()
            self.db.add(
                MissionSuccessCriterionVersion(
                    id=self.uid("mission_criterion_version"),
                    mission_version_id=mission.id,
                    success_criterion_id=criterion.id,
                    position=position,
                    statement=(
                        f"完成《{book.title}》的综合任务，并能解释关键机制、边界与迁移方式"
                    ),
                    acceptance_json=_dump({
                        "evidenceRule": "book_capstone_completed",
                        "bookPosition": position,
                    }),
                    provenance_json=_dump({"mode": "derived_from_generated_book"}),
                )
            )
        return mission

    def record_initial_adoption(
        self,
        *,
        run: LearningRun,
        mission: LearningMissionVersion,
        source: str,
    ) -> MissionAdoptionEvent:
        event = MissionAdoptionEvent(
            id=self.uid("mission_adoption"),
            learning_run_id=run.id,
            user_id=self.user_id,
            mission_version_id=mission.id,
            previous_mission_version_id=None,
            event_type="initialized",
            source=source,
            reason="学习运行采用其创建时的任务版本",
            idempotency_key=f"initial:{run.id}:{mission.id}",
        )
        self.db.add(event)
        return event

    def create_draft(self, series_id: str, body) -> dict:
        series, run = self._owned_series_and_run(series_id)
        current = self._current_version(series, run)
        if body.expected_current_mission_version_id != current.id:
            raise AppError(
                "学习任务版本已经变化，请刷新后重试",
                code="MISSION_VERSION_CONFLICT",
                status=409,
            )
        criterion_keys = [item.key for item in body.success_criteria]
        if len(set(criterion_keys)) != len(criterion_keys):
            raise AppError(
                "成功标准标识不能重复",
                code="MISSION_CRITERION_DUPLICATE",
                status=400,
            )
        payload = {
            "why": body.why.strip(),
            "targetCapabilities": body.target_capabilities,
            "constraints": body.constraints,
            "outOfScope": body.out_of_scope,
            "assumptions": body.assumptions,
            "learnerContext": body.learner_context,
            "inferredFields": body.inferred_fields,
            "schemaVersion": SCHEMA_VERSION,
            "successCriteria": [
                item.model_dump(by_alias=False) for item in body.success_criteria
            ],
        }
        digest = _payload_hash(payload)
        existing = self.db.scalar(
            select(LearningMissionVersion).where(
                LearningMissionVersion.plan_id == series.plan_id,
                LearningMissionVersion.payload_hash == digest,
            )
        )
        if existing:
            return self._version_view(series, run, existing)
        version_number = (
            self.db.scalar(
                select(func.max(LearningMissionVersion.version)).where(
                    LearningMissionVersion.plan_id == series.plan_id
                )
            )
            or 0
        ) + 1
        mission = LearningMissionVersion(
            id=self.uid("mission_version"),
            plan_id=series.plan_id,
            user_id=self.user_id,
            version=version_number,
            status="draft",
            why=payload["why"],
            target_capabilities_json=_dump(payload["targetCapabilities"]),
            constraints_json=_dump(payload["constraints"]),
            out_of_scope_json=_dump(payload["outOfScope"]),
            assumptions_json=_dump(payload["assumptions"]),
            learner_context_json=_dump(payload["learnerContext"]),
            inferred_fields_json=_dump(payload["inferredFields"]),
            provenance_json=_dump({
                "mode": "user_revision",
                "sourceMissionVersionId": current.id,
            }),
            schema_version=SCHEMA_VERSION,
            payload_hash=digest,
            supersedes_id=current.id,
        )
        self.db.add(mission)
        self.db.flush()
        for position, item in enumerate(body.success_criteria, 1):
            identity = self.db.scalar(
                select(MissionSuccessCriterion).where(
                    MissionSuccessCriterion.plan_id == series.plan_id,
                    MissionSuccessCriterion.stable_key == item.key,
                )
            )
            if not identity:
                identity = MissionSuccessCriterion(
                    id=self.uid("mission_criterion"),
                    plan_id=series.plan_id,
                    stable_key=item.key,
                )
                self.db.add(identity)
                self.db.flush()
            self.db.add(
                MissionSuccessCriterionVersion(
                    id=self.uid("mission_criterion_version"),
                    mission_version_id=mission.id,
                    success_criterion_id=identity.id,
                    position=position,
                    statement=item.statement.strip(),
                    acceptance_json=_dump(item.acceptance),
                    provenance_json=_dump({"mode": "user_revision"}),
                )
            )
        self.db.flush()
        return self._version_view(series, run, mission)

    def confirm(self, series_id: str, mission_version_id: str) -> dict:
        series, run = self._owned_series_and_run(series_id)
        mission = self.db.get(LearningMissionVersion, mission_version_id)
        if not mission or mission.plan_id != series.plan_id:
            raise AppError("学习任务版本不存在", code="MISSION_VERSION_NOT_FOUND", status=404)
        if mission.status == "draft":
            if _load(mission.inferred_fields_json, []):
                raise AppError(
                    "仍有推断字段需要用户确认",
                    code="MISSION_INFERRED_FIELDS_UNCONFIRMED",
                    status=409,
                )
            mission.status = "confirmed"
            mission.confirmed_at = now()
        elif mission.status not in {"confirmed", "grandfathered_m1"}:
            raise AppError("该任务版本不能确认", code="MISSION_CONFIRM_INVALID", status=409)
        self.db.flush()
        return self._version_view(series, run, mission)

    def adopt(self, series_id: str, body, *, idempotency_key: str | None) -> dict:
        series, run = self._owned_series_and_run(series_id)
        if not run:
            raise AppError("学习运行不存在", code="LEARNING_RUN_MISSING", status=409)
        key = (idempotency_key or self.uid("mission_adoption_request")).strip()
        if not 8 <= len(key) <= 160:
            raise AppError("采用请求标识无效", code="IDEMPOTENCY_KEY_INVALID", status=400)
        existing = self.db.scalar(
            select(MissionAdoptionEvent).where(
                MissionAdoptionEvent.learning_run_id == run.id,
                MissionAdoptionEvent.user_id == self.user_id,
                MissionAdoptionEvent.idempotency_key == key,
            )
        )
        if existing:
            if existing.mission_version_id != body.mission_version_id:
                raise AppError(
                    "采用请求标识已用于其他任务版本",
                    code="IDEMPOTENCY_KEY_REUSED",
                    status=409,
                )
            return self.view(series_id)
        current = self._current_version(series, run)
        if body.expected_current_mission_version_id != current.id:
            raise AppError(
                "学习任务版本已经变化，请刷新后重试",
                code="MISSION_VERSION_CONFLICT",
                status=409,
            )
        candidate = self.db.get(LearningMissionVersion, body.mission_version_id)
        if not candidate or candidate.plan_id != series.plan_id:
            raise AppError("学习任务版本不存在", code="MISSION_VERSION_NOT_FOUND", status=404)
        if candidate.status not in {"confirmed", "grandfathered_m1"}:
            raise AppError(
                "只能采用已确认的任务版本",
                code="MISSION_VERSION_NOT_CONFIRMED",
                status=409,
            )
        self.db.add(
            MissionAdoptionEvent(
                id=self.uid("mission_adoption"),
                learning_run_id=run.id,
                user_id=self.user_id,
                mission_version_id=candidate.id,
                previous_mission_version_id=current.id,
                event_type="mission_adopted",
                source="user",
                reason=body.reason.strip(),
                idempotency_key=key,
            )
        )
        self.db.flush()
        return self.view(series_id)

    def view(self, series_id: str) -> dict:
        series, run = self._owned_series_and_run(series_id)
        mission = self._current_version(series, run)
        return self._version_view(series, run, mission)

    def current_version(self, series_id: str) -> LearningMissionVersion:
        series, run = self._owned_series_and_run(series_id)
        return self._current_version(series, run)

    def _owned_series_and_run(
        self, series_id: str
    ) -> tuple[Series, LearningRun | None]:
        series = self.db.scalar(
            select(Series)
            .join(Shelf, Shelf.id == Series.shelf_id)
            .where(
                Series.id == series_id,
                Series.deleted_at.is_(None),
                Shelf.user_id == self.user_id,
            )
        )
        if not series:
            raise AppError("学习任务不存在", code="MISSION_NOT_FOUND", status=404)
        run = self.db.scalar(
            select(LearningRun)
            .where(
                LearningRun.user_id == self.user_id,
                LearningRun.series_id == series_id,
                LearningRun.status == "active",
            )
            .order_by(LearningRun.created_at.desc())
        )
        return series, run

    def _current_version(
        self, series: Series, run: LearningRun | None
    ) -> LearningMissionVersion:
        mission_id = series.initial_mission_version_id
        if run:
            adoption = self.db.scalar(
                select(MissionAdoptionEvent)
                .where(
                    MissionAdoptionEvent.learning_run_id == run.id,
                    MissionAdoptionEvent.user_id == self.user_id,
                )
                .order_by(MissionAdoptionEvent.sequence.desc())
            )
            if adoption:
                mission_id = adoption.mission_version_id
        mission = self.db.get(LearningMissionVersion, mission_id) if mission_id else None
        if not mission or mission.user_id != self.user_id:
            raise AppError("学习任务版本缺失", code="MISSION_VERSION_MISSING", status=500)
        return mission

    def _version_view(
        self,
        series: Series,
        run: LearningRun | None,
        mission: LearningMissionVersion,
    ) -> dict:
        adoption = None
        if run:
            adoption = self.db.scalar(
                select(MissionAdoptionEvent)
                .where(
                    MissionAdoptionEvent.learning_run_id == run.id,
                    MissionAdoptionEvent.user_id == self.user_id,
                    MissionAdoptionEvent.mission_version_id == mission.id,
                )
                .order_by(MissionAdoptionEvent.sequence.desc())
            )
        criteria = self.db.execute(
            select(MissionSuccessCriterionVersion, MissionSuccessCriterion)
            .join(
                MissionSuccessCriterion,
                MissionSuccessCriterion.id
                == MissionSuccessCriterionVersion.success_criterion_id,
            )
            .where(MissionSuccessCriterionVersion.mission_version_id == mission.id)
            .order_by(MissionSuccessCriterionVersion.position)
        ).all()
        return {
            "id": mission.id,
            "seriesId": series.id,
            "planId": mission.plan_id,
            "version": mission.version,
            "status": mission.status,
            "why": mission.why,
            "targetCapabilities": _load(mission.target_capabilities_json, []),
            "constraints": _load(mission.constraints_json, {}),
            "outOfScope": _load(mission.out_of_scope_json, []),
            "assumptions": _load(mission.assumptions_json, []),
            "learnerContext": _load(mission.learner_context_json, {}),
            "inferredFields": _load(mission.inferred_fields_json, []),
            "provenance": _load(mission.provenance_json, {}),
            "schemaVersion": mission.schema_version,
            "successCriteria": [
                {
                    "id": criterion.id,
                    "key": identity.stable_key,
                    "position": criterion.position,
                    "statement": criterion.statement,
                    "acceptance": _load(criterion.acceptance_json, {}),
                    "provenance": _load(criterion.provenance_json, {}),
                }
                for criterion, identity in criteria
            ],
            "adoption": {
                "eventId": adoption.id if adoption else None,
                "learningRunId": run.id if run else None,
                "source": adoption.source if adoption else "series_initial_binding",
            },
            "confirmedAt": mission.confirmed_at.isoformat() if mission.confirmed_at else None,
            "createdAt": mission.created_at.isoformat(),
        }
