from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr


def camel(value: str):
    first, *rest = value.split("_")
    return first + "".join(x.title() for x in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=camel, populate_by_name=True)


class ShelfCreate(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    domain: str = Field(default="", max_length=100)
    specialty: str = Field(default="", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=12)


class PlanCreate(ApiModel):
    shelf_id: str
    topic: str = Field(min_length=1, max_length=160)
    role: str = Field(
        min_length=1,
        max_length=80,
        description="学习者的专业、身份或当前背景",
    )
    experience: str = Field(min_length=1, max_length=500)
    purpose: str = Field(default="", max_length=1000)
    depth: Literal["overview", "deep", "mastery"]
    details: str = Field(default="", max_length=3000)


class MissionCriterionInput(ApiModel):
    key: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=2000)
    acceptance: dict = Field(default_factory=dict)


class MissionVersionCreate(ApiModel):
    expected_current_mission_version_id: str
    why: str = Field(min_length=1, max_length=3000)
    target_capabilities: list[dict] = Field(min_length=1, max_length=30)
    constraints: dict = Field(default_factory=dict)
    out_of_scope: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    learner_context: dict = Field(default_factory=dict)
    inferred_fields: list[str] = Field(default_factory=list, max_length=30)
    success_criteria: list[MissionCriterionInput] = Field(min_length=1, max_length=30)


class MissionAdoptionCreate(ApiModel):
    mission_version_id: str
    expected_current_mission_version_id: str
    reason: str = Field(min_length=1, max_length=2000)


class AiRuntimeUpdate(ApiModel):
    mode: Literal["provider", "demo"] = "provider"
    provider_protocol: Literal["openai", "anthropic"] = "openai"
    api_key: SecretStr | None = None
    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="gpt-5", min_length=1, max_length=160)
    api_mode: Literal["responses", "chat_completions"] = "responses"
    reasoning_mode: Literal["optional", "required", "disabled"] = "optional"


class PasswordLogin(ApiModel):
    username: str = Field(min_length=3, max_length=80)
    password: SecretStr = Field(min_length=8, max_length=200)


ProfileStage = Literal[
    "exploring",
    "beginner",
    "foundation",
    "practice",
    "advanced",
]


class ProfileDraftUpdate(ApiModel):
    current_step: Literal["identity", "direction", "review"]
    profession: str | None = Field(default=None, max_length=120)
    stage: ProfileStage | None = None
    purpose: str | None = Field(default=None, max_length=1000)
    domains: list[str] | None = Field(default=None, max_length=6)
    experience: str | None = Field(default=None, max_length=1000)


class ProfileComplete(ApiModel):
    profession: str = Field(min_length=1, max_length=120)
    stage: ProfileStage
    purpose: str = Field(min_length=1, max_length=1000)
    domains: list[str] = Field(min_length=1, max_length=6)
    experience: str = Field(default="", max_length=1000)
    weekly_minutes: int = Field(default=0, ge=0, le=10080)
    target_date: str = Field(default="", max_length=10, pattern=r"^$|^\d{4}-\d{2}-\d{2}$")


class QuizSubmit(ApiModel):
    quiz_set_id: str
    answers: list[list[int]]


class ReviewSubmit(ApiModel):
    answers: list[list[int]] = Field(min_length=1, max_length=5)


class AskRequest(ApiModel):
    block_id: str
    question: str = Field(min_length=1, max_length=3000)
    thread_id: str | None = None
    force_relation: Literal["follow_up", "new_question"] | None = None


class QaClassificationUpdate(ApiModel):
    relation: Literal["follow_up", "new_question"]
    target_thread_id: str | None = None


class NoteUpdate(ApiModel):
    content: dict


class NoteReviewSupplementCreate(ApiModel):
    review_episode_id: str = Field(min_length=8, max_length=120)
    content: dict


class ResumeUpdate(ApiModel):
    block_id: str = Field(default="", max_length=200)


class AskMeReply(ApiModel):
    answer: str = Field(default="", max_length=3000)


class AttachmentSubmit(ApiModel):
    content: dict
    attachment_ids: list[str] = Field(min_length=1, max_length=20)


class ChapterCreate(ApiModel):
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=2000)
    position: int | None = Field(default=None, ge=1)


class ChapterUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    objective: str | None = Field(default=None, min_length=1, max_length=2000)


class ChapterOrder(ApiModel):
    chapter_ids: list[str] = Field(min_length=1)
