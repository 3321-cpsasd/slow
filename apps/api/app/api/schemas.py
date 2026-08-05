from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


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


class LearningPreferences(ApiModel):
    opening_style: Literal[
        "auto", "problem_first", "example_first", "concept_first"
    ] = "auto"
    explanation_density: Literal[
        "auto", "concise", "balanced", "thorough"
    ] = "auto"
    format_preferences: list[
        Literal["diagram", "worked_example", "code", "table", "analogy"]
    ] = Field(default_factory=list, max_length=5)
    interaction_rhythm: Literal[
        "auto", "low_interruption", "balanced", "frequent_checkins"
    ] = "auto"


class ProfileDraftUpdate(ApiModel):
    current_step: Literal["identity", "direction", "review"]
    profession: str | None = Field(default=None, max_length=120)
    stage: ProfileStage | None = None
    purpose: str | None = Field(default=None, max_length=1000)
    domains: list[str] | None = Field(default=None, max_length=6)
    experience: str | None = Field(default=None, max_length=1000)
    preferences: LearningPreferences | None = None


class ProfileComplete(ApiModel):
    profession: str = Field(min_length=1, max_length=120)
    stage: ProfileStage
    purpose: str = Field(min_length=1, max_length=1000)
    domains: list[str] = Field(min_length=1, max_length=6)
    experience: str = Field(default="", max_length=1000)
    weekly_minutes: int = Field(default=0, ge=0, le=10080)
    target_date: str = Field(default="", max_length=10, pattern=r"^$|^\d{4}-\d{2}-\d{2}$")
    preferences: LearningPreferences = Field(default_factory=LearningPreferences)


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


class FeedbackCreate(ApiModel):
    scope: Literal["global", "content_block"]
    feedback_type: Literal[
        "inaccurate",
        "unclear",
        "poor_example",
        "typo",
        "layout",
        "bug",
        "feature",
        "experience",
        "other",
    ]
    message: str = Field(default="", max_length=4000)
    page_path: str = Field(default="/", max_length=500)
    view: Literal["", "home", "shelf", "learn", "profile"] = ""
    section_id: str | None = Field(default=None, max_length=160)
    content_version_id: str | None = Field(default=None, max_length=160)
    block_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_scope(self):
        paragraph_types = {
            "inaccurate", "unclear", "poor_example", "typo", "layout", "other"
        }
        global_types = {"bug", "feature", "experience", "other"}
        allowed = paragraph_types if self.scope == "content_block" else global_types
        if self.feedback_type not in allowed:
            raise ValueError("反馈类型与反馈范围不匹配")
        if self.scope == "content_block":
            if not self.section_id or not self.content_version_id or not self.block_id:
                raise ValueError("按段反馈必须绑定小节、内容版本和段落")
        elif self.section_id or self.content_version_id or self.block_id:
            raise ValueError("全局反馈不能绑定正文段落")
        self.message = self.message.strip()
        if self.scope == "global" and len(self.message) < 2:
            raise ValueError("请补充至少两个字的反馈说明")
        if self.feedback_type == "other" and len(self.message) < 2:
            raise ValueError("选择其他时请补充反馈说明")
        return self


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
