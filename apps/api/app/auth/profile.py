import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import (
    UserOnboarding,
    UserProfile,
    UserProfileRevision,
    now,
)


PROFILE_FLOW_ID = "baseline_profile"
PROFILE_FLOW_VERSION = 1
PROFILE_STEPS = (
    {
        "id": "identity",
        "title": "现在的你",
        "description": "职业身份与当前学习阶段",
    },
    {
        "id": "direction",
        "title": "想去的地方",
        "description": "目标领域、已有经验与学习目的",
    },
    {
        "id": "review",
        "title": "准备开始",
        "description": "确认起点并创建第一个书架",
    },
)
PROFILE_STEP_IDS = {item["id"] for item in PROFILE_STEPS}
DEFAULT_LEARNING_PREFERENCES = {
    "openingStyle": "auto",
    "explanationDensity": "auto",
    "formatPreferences": [],
    "interactionRhythm": "auto",
    "dailyModePromptEnabled": False,
}
PREFERENCE_VALUES = {
    "openingStyle": {"auto", "problem_first", "example_first", "concept_first"},
    "explanationDensity": {"auto", "concise", "balanced", "thorough"},
    "formatPreferences": {"diagram", "worked_example", "code", "table", "analogy"},
    "interactionRhythm": {"auto", "low_interruption", "balanced", "frequent_checkins"},
}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class ProfileService:
    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id

    def is_complete(self) -> bool:
        profile = self.db.get(UserProfile, self.user_id)
        return bool(profile and profile.completed_at)

    def require_complete(self) -> None:
        if not self.is_complete():
            raise AppError(
                "请先完成基础学习画像",
                code="PROFILE_REQUIRED",
                status=428,
            )

    def state(self) -> dict:
        profile = self.db.get(UserProfile, self.user_id)
        flow = self.db.scalar(
            select(UserOnboarding).where(
                UserOnboarding.user_id == self.user_id,
                UserOnboarding.flow_id == PROFILE_FLOW_ID,
            )
        )
        complete = bool(profile and profile.completed_at)
        current_step = (
            "review"
            if complete
            else flow.current_step
            if flow and flow.current_step in PROFILE_STEP_IDS
            else "identity"
        )
        return {
            "flowId": PROFILE_FLOW_ID,
            "flowVersion": PROFILE_FLOW_VERSION,
            "required": not complete,
            "status": "completed" if complete else "required",
            "currentStep": current_step,
            "steps": list(PROFILE_STEPS),
            "profile": self._profile_view(profile),
        }

    def save_draft(self, *, current_step: str, values: dict) -> dict:
        if current_step not in PROFILE_STEP_IDS:
            raise AppError(
                "画像引导步骤无效",
                code="PROFILE_STEP_INVALID",
                status=400,
            )
        profile = self._profile(create=True)
        self._apply(profile, values)
        flow = self._flow(create=True)
        flow.current_step = current_step
        flow.status = "completed" if profile.completed_at else "required"
        flow.updated_at = now()
        self.db.commit()
        return self.state()

    def complete(
        self,
        values: dict,
        *,
        source: str = "self_report",
        commit: bool = True,
    ) -> dict:
        profile = self._profile(create=True)
        self._apply(profile, values)
        domains = self._normalized_domains(profile.domains_json)
        if not all(
            (
                profile.profession.strip(),
                profile.stage.strip(),
                profile.purpose.strip(),
                domains,
            )
        ):
            raise AppError(
                "职业、阶段、学习目的和目标领域均为必填项",
                code="PROFILE_INCOMPLETE",
                status=400,
            )
        completed_at = now()
        profile.version += 1
        profile.completed_at = completed_at
        profile.updated_at = completed_at
        snapshot = self._profile_view(profile)
        self.db.add(
            UserProfileRevision(
                id=f"profile_revision_{uuid4().hex}",
                user_id=self.user_id,
                version=profile.version,
                snapshot_json=_dump(snapshot),
                source=source,
            )
        )
        flow = self._flow(create=True)
        flow.current_step = "review"
        flow.status = "completed"
        flow.completed_at = completed_at
        flow.updated_at = completed_at
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return self.state()

    def seed_complete(
        self,
        *,
        profession: str,
        stage: str,
        purpose: str,
        domains: list[str],
        experience: str,
    ) -> None:
        if self.is_complete():
            return
        self.complete(
            {
                "profession": profession,
                "stage": stage,
                "purpose": purpose,
                "domains": domains,
                "experience": experience,
            },
            source="system_seed",
        )

    def _profile(self, *, create: bool) -> UserProfile | None:
        profile = self.db.get(UserProfile, self.user_id)
        if not profile and create:
            profile = UserProfile(user_id=self.user_id)
            self.db.add(profile)
            self.db.flush()
        return profile

    def _flow(self, *, create: bool) -> UserOnboarding | None:
        flow = self.db.scalar(
            select(UserOnboarding).where(
                UserOnboarding.user_id == self.user_id,
                UserOnboarding.flow_id == PROFILE_FLOW_ID,
            )
        )
        if not flow and create:
            flow = UserOnboarding(
                id=f"onboarding_{uuid4().hex}",
                user_id=self.user_id,
                flow_id=PROFILE_FLOW_ID,
                flow_version=PROFILE_FLOW_VERSION,
                status="required",
                current_step="identity",
            )
            self.db.add(flow)
            self.db.flush()
        return flow

    @staticmethod
    def _normalized_domains(value: str) -> list[str]:
        result = []
        for item in _load(value, []):
            normalized = str(item).strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result[:6]

    def _apply(self, profile: UserProfile, values: dict) -> None:
        for field in ("profession", "stage", "purpose", "experience", "target_date"):
            value = values.get(field)
            if value is not None:
                setattr(profile, field, str(value).strip())
        if values.get("weekly_minutes") is not None:
            profile.weekly_minutes = max(0, min(10080, int(values["weekly_minutes"])))
        if values.get("domains") is not None:
            profile.domains_json = _dump(
                self._normalized_domains(_dump(values["domains"]))
            )
        if values.get("preferences") is not None:
            profile.preferences_json = _dump(
                self._normalized_preferences(values["preferences"])
            )
        profile.updated_at = now()

    @staticmethod
    def _normalized_preferences(value) -> dict:
        if isinstance(value, str):
            value = _load(value, {})
        value = value if isinstance(value, dict) else {}
        result = dict(DEFAULT_LEARNING_PREFERENCES)
        aliases = {
            "openingStyle": ("openingStyle", "opening_style"),
            "explanationDensity": ("explanationDensity", "explanation_density"),
            "formatPreferences": ("formatPreferences", "format_preferences"),
            "interactionRhythm": ("interactionRhythm", "interaction_rhythm"),
            "dailyModePromptEnabled": (
                "dailyModePromptEnabled",
                "daily_mode_prompt_enabled",
            ),
        }
        for key in ("openingStyle", "explanationDensity", "interactionRhythm"):
            candidate = next(
                (value.get(alias) for alias in aliases[key] if value.get(alias)),
                "auto",
            )
            if candidate in PREFERENCE_VALUES[key]:
                result[key] = candidate
        formats = next(
            (value.get(alias) for alias in aliases["formatPreferences"] if value.get(alias) is not None),
            [],
        )
        result["formatPreferences"] = [
            item
            for item in dict.fromkeys(formats if isinstance(formats, list) else [])
            if item in PREFERENCE_VALUES["formatPreferences"]
        ][:5]
        prompt_enabled = next(
            (
                value.get(alias)
                for alias in aliases["dailyModePromptEnabled"]
                if value.get(alias) is not None
            ),
            False,
        )
        if isinstance(prompt_enabled, bool):
            result["dailyModePromptEnabled"] = prompt_enabled
        return result

    def _profile_view(self, profile: UserProfile | None) -> dict:
        if not profile:
            return {
                "profession": "",
                "stage": "",
                "purpose": "",
                "domains": [],
                "experience": "",
                "weeklyMinutes": 0,
                "targetDate": "",
                "preferences": dict(DEFAULT_LEARNING_PREFERENCES),
                "version": 0,
                "completedAt": None,
            }
        return {
            "profession": profile.profession,
            "stage": profile.stage,
            "purpose": profile.purpose,
            "domains": self._normalized_domains(profile.domains_json),
            "experience": profile.experience,
            "weeklyMinutes": profile.weekly_minutes,
            "targetDate": profile.target_date,
            "preferences": self._normalized_preferences(profile.preferences_json),
            "version": profile.version,
            "completedAt": (
                profile.completed_at.isoformat()
                if profile.completed_at
                else None
            ),
        }
