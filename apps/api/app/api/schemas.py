from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def camel(value: str):
    first, *rest = value.split("_")
    return first + "".join(x.title() for x in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=camel, populate_by_name=True)


class ShelfCreate(ApiModel):
    model_config = ConfigDict(
        alias_generator=camel,
        populate_by_name=True,
        extra="forbid",
    )
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str):
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("书架名称不能为空")
        return normalized


LearningStartPreferenceKey = Literal[
    "practical_application",
    "understand_principles",
    "case_based",
    "practice_heavy",
]


class LearningStartSelection(ApiModel):
    preview_id: str = Field(min_length=1, max_length=160)
    selected_concept_revision_ids: list[str] = Field(min_length=1, max_length=120)
    learning_preferences: list[LearningStartPreferenceKey] = Field(
        default_factory=list, max_length=2
    )

    @model_validator(mode="after")
    def selection_is_unique(self):
        if len(self.selected_concept_revision_ids) != len(
            set(self.selected_concept_revision_ids)
        ):
            raise ValueError("点亮的知识方向不能重复")
        if len(self.learning_preferences) != len(set(self.learning_preferences)):
            raise ValueError("学习偏好不能重复")
        return self


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
    start_mode: Literal["direct", "guided"] = "direct"
    learning_start_selection: LearningStartSelection | None = None

    @model_validator(mode="after")
    def guided_start_requires_selection(self):
        if self.start_mode == "guided" and self.learning_start_selection is None:
            raise ValueError("先挑重点需要至少点亮一个知识方向")
        if self.start_mode == "direct" and self.learning_start_selection is not None:
            raise ValueError("直接开始不能附带知识版图选择")
        return self


class LearningStartPreviewCreate(ApiModel):
    shelf_id: str
    topic: str = Field(min_length=1, max_length=160)
    role: str = Field(min_length=1, max_length=80)
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


class AiDeploymentUpdate(ApiModel):
    deployment_id: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    provider_id: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    model: str = Field(min_length=1, max_length=160)
    model_family_id: str = Field(
        min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    provider_protocol: Literal["openai", "anthropic"] = "openai"
    api_mode: Literal["responses", "chat_completions", "messages"] = (
        "chat_completions"
    )
    reasoning_mode: Literal["optional", "required", "disabled"] = "optional"
    api_key: SecretStr | None = None
    base_url: str = Field(default="", max_length=1000)
    structured_mode: Literal[
        "native_schema", "json_object", "prompt_json", "unsupported"
    ] = "unsupported"
    streaming: bool = True
    backend_allowed: bool = False
    allowed_environments: list[
        Literal["development", "demo", "test", "production"]
    ] = Field(
        default_factory=lambda: ["development", "test"],
        min_length=1,
    )
    status: Literal["active", "quarantined", "disabled"] = "active"


class AiRuntimeUpdate(ApiModel):
    mode: Literal["provider", "demo"] = "provider"
    provider_protocol: Literal["openai", "anthropic"] = "openai"
    api_key: SecretStr | None = None
    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="gpt-5", min_length=1, max_length=160)
    api_mode: Literal["responses", "chat_completions"] = "responses"
    reasoning_mode: Literal["optional", "required", "disabled"] = "optional"
    deployments: list[AiDeploymentUpdate] | None = None
    routes: dict[str, list[str]] | None = None
    route_policy_version: str = Field(default="ai_route_v1", max_length=80)

    @model_validator(mode="after")
    def validate_ai_routes(self):
        allowed_purposes = {
            "default",
            "curriculum",
            "curriculum_review",
            "lesson_author",
            "assessment_item_author",
            "assessment_item_review",
            "assessment_answer_adjudication",
            "ask_ai",
            "feedback_style",
            "feedback_accuracy",
            "assessment_probe",
            "assessment_evaluation",
            "note",
            "source_repair",
            "source_review",
            "quality_review",
        }
        if self.deployments is None and self.routes is not None:
            raise ValueError("更新用途路由时必须同时提交完整模型部署池")
        if self.deployments is not None:
            identifiers = [item.deployment_id for item in self.deployments]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("模型部署 ID 不能重复")
            known = set(identifiers)
            for purpose, deployment_ids in (self.routes or {}).items():
                if purpose not in allowed_purposes:
                    raise ValueError(f"不支持的用途路由 {purpose}")
                if not deployment_ids:
                    raise ValueError(f"用途路由 {purpose} 不能为空")
                if any(item not in known for item in deployment_ids):
                    raise ValueError(f"用途路由 {purpose} 引用了未知部署")
        return self


class PasswordLogin(ApiModel):
    username: str = Field(min_length=3, max_length=80)
    password: SecretStr = Field(min_length=8, max_length=200)


class PasswordRegistration(ApiModel):
    username: str = Field(min_length=3, max_length=80)
    password: SecretStr = Field(min_length=12, max_length=200)
    password_confirm: SecretStr = Field(min_length=12, max_length=200)
    alpha_code: SecretStr | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def passwords_match(self):
        if self.password.get_secret_value() != self.password_confirm.get_secret_value():
            raise ValueError("两次输入的密码不一致")
        return self


class PasswordRecoveryReset(ApiModel):
    username: str = Field(min_length=3, max_length=80)
    recovery_code: SecretStr = Field(min_length=20, max_length=100)
    new_password: SecretStr = Field(min_length=12, max_length=200)
    new_password_confirm: SecretStr = Field(min_length=12, max_length=200)

    @model_validator(mode="after")
    def passwords_match(self):
        if (
            self.new_password.get_secret_value()
            != self.new_password_confirm.get_secret_value()
        ):
            raise ValueError("两次输入的新密码不一致")
        return self


class RecoveryCodeRotate(ApiModel):
    current_password: SecretStr = Field(min_length=8, max_length=200)


class PrivacyConsentCreate(ApiModel):
    privacy_accepted: bool = Field(alias="privacyAccepted")
    trial_accepted: bool = Field(alias="trialAccepted")


class AccountExitCreate(ApiModel):
    confirmation: str = Field(min_length=1, max_length=20)
    reason: str = Field(default="", max_length=500)


class DailyModeUpdate(ApiModel):
    model_config = ConfigDict(
        alias_generator=camel,
        populate_by_name=True,
        extra="forbid",
    )

    daily_mode: Literal["fast", "slow"]
    duration: Literal["1h", "3h", "6h", "today"]
    timezone: str = Field(min_length=1, max_length=64)
    source: Literal["dialog", "header_toggle", "duration_adjustment"]


ProductEventName = Literal[
    "home_viewed",
    "shelf_viewed",
    "learning_viewed",
    "profile_viewed",
    "review_center_viewed",
    "section_viewed",
    "quiz_viewed",
    "feedback_opened",
    "explanation_style_requested",
    "explanation_style_feedback",
    "explanation_style_remembered",
    "active_reading_60s",
    "frontend_error",
]


class ProductEventCreate(ApiModel):
    model_config = ConfigDict(
        alias_generator=camel,
        populate_by_name=True,
        extra="forbid",
    )

    event_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    event_name: ProductEventName
    occurred_at: datetime
    page_path: str = Field(default="/", min_length=1, max_length=500)
    view: Literal["", "home", "shelf", "learn", "profile", "knowledge", "review"] = ""
    entity_type: Literal["", "shelf", "series", "book", "chapter", "section"] = ""
    entity_id: str = Field(default="", max_length=160, pattern=r"^[A-Za-z0-9_.:-]*$")
    properties: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("page_path")
    @classmethod
    def validate_page_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "\\" in value or "\x00" in value:
            raise ValueError("pagePath 必须是站内路径")
        return value.split("?", 1)[0].split("#", 1)[0] or "/"


class ProductEventBatch(ApiModel):
    model_config = ConfigDict(
        alias_generator=camel,
        populate_by_name=True,
        extra="forbid",
    )

    events: list[ProductEventCreate] = Field(min_length=1, max_length=25)


class StudyActivityHeartbeat(ApiModel):
    model_config = ConfigDict(
        alias_generator=camel,
        populate_by_name=True,
        extra="forbid",
    )

    event_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    client_session_id: str = Field(
        min_length=8,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    client_sequence: int = Field(ge=0, le=2_147_483_647)
    activity_kind: Literal[
        "reading_thinking",
        "verification_review",
        "ask_ai",
    ]
    section_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    timezone: str = Field(min_length=1, max_length=64)


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
    daily_mode_prompt_enabled: bool = Field(default=False, strict=True)


class LearningPreferenceEvidenceCreate(ApiModel):
    event_id: str = Field(min_length=8, max_length=128)
    request_event_id: str | None = Field(default=None, min_length=8, max_length=128)
    section_id: str = Field(min_length=1, max_length=160)
    content_version_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    block_kind: Literal["text", "bullet_list", "ordered_steps", "diagram", "table", "code", "formula"]
    style: Literal["worked_example", "diagram", "analogy", "derivation", "precise", "concise", "custom"]
    signal: Literal["requested", "helpful", "unclear"]
    custom_instruction: str | None = Field(default=None, max_length=240)


class LearningPreferenceDecisionCreate(ApiModel):
    decision_key: str = Field(min_length=8, max_length=128)
    request_event_id: str = Field(min_length=8, max_length=128)
    dimension: Literal[
        "example", "diagram", "analogy", "derivation", "precision", "concise",
        "plain_language", "humor",
    ]
    scope_kind: Literal["global", "shelf"] = "shelf"
    shelf_id: str | None = Field(default=None, min_length=1, max_length=160)
    state: Literal["confirmed", "cleared"] = "confirmed"

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.scope_kind == "shelf") != bool(self.shelf_id):
            raise ValueError("书架范围的偏好决定必须绑定书架")
        return self


class PersonalPresentationAdopt(ApiModel):
    event_id: str = Field(min_length=8, max_length=128)
    request_event_id: str = Field(min_length=8, max_length=128)
    content_version_id: str = Field(min_length=1, max_length=160)
    block_id: str = Field(min_length=1, max_length=160)
    block_kind: Literal["text", "bullet_list", "ordered_steps", "diagram", "table", "code", "formula"]
    style: Literal["worked_example", "diagram", "analogy", "derivation", "precise", "concise", "custom"]
    thread_id: str = Field(min_length=1, max_length=160)
    answer_message_id: str = Field(min_length=1, max_length=160)


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


class ReinforcementRespond(ApiModel):
    activity_key: str = Field(min_length=1, max_length=64)
    selected_options: list[int] = Field(default_factory=list, max_length=6)
    response_text: str = Field(default="", max_length=2000)
    acknowledged: bool = False


class AskRequest(ApiModel):
    block_id: str
    question: str = Field(min_length=1, max_length=3000)
    thread_id: str | None = None
    force_relation: Literal["follow_up", "new_question"] | None = None
    preference_request_event_id: str | None = Field(default=None, max_length=128)
    explanation_style: Literal[
        "worked_example", "diagram", "analogy", "derivation", "precise",
        "concise", "custom",
    ] | None = None
    explanation_block_kind: Literal[
        "text", "bullet_list", "ordered_steps", "diagram", "table", "code",
        "formula",
    ] | None = None

    @model_validator(mode="after")
    def validate_explanation_lineage(self):
        values = (
            self.preference_request_event_id,
            self.explanation_style,
            self.explanation_block_kind,
        )
        if any(values) and not all(values):
            raise ValueError("讲法请求的偏好元数据必须完整")
        return self


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
    scope: Literal["global", "content_block", "quiz_question"]
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
    view: Literal["", "home", "shelf", "learn", "profile", "knowledge", "review"] = ""
    section_id: str | None = Field(default=None, max_length=160)
    content_version_id: str | None = Field(default=None, max_length=160)
    block_id: str | None = Field(default=None, max_length=160)
    attempt_id: str | None = Field(default=None, max_length=160)
    question_index: int | None = Field(default=None, ge=0, le=11)

    @model_validator(mode="after")
    def validate_scope(self):
        paragraph_types = {
            "inaccurate", "unclear", "poor_example", "typo", "layout", "other"
        }
        question_types = {"inaccurate", "unclear"}
        global_types = {"bug", "feature", "experience", "other"}
        allowed = (
            paragraph_types
            if self.scope == "content_block"
            else question_types
            if self.scope == "quiz_question"
            else global_types
        )
        if self.feedback_type not in allowed:
            raise ValueError("反馈类型与反馈范围不匹配")
        if self.scope == "content_block":
            if not self.section_id or not self.content_version_id or not self.block_id:
                raise ValueError("按段反馈必须绑定小节、内容版本和段落")
            if self.attempt_id is not None or self.question_index is not None:
                raise ValueError("按段反馈不能绑定作答题目")
        elif self.scope == "quiz_question":
            if not self.section_id or not self.attempt_id or self.question_index is None:
                raise ValueError("错题反馈必须绑定小节、作答记录和题号")
            if self.content_version_id or self.block_id:
                raise ValueError("错题反馈不能绑定正文段落")
        elif (
            self.section_id
            or self.content_version_id
            or self.block_id
            or self.attempt_id
            or self.question_index is not None
        ):
            raise ValueError("全局反馈不能绑定学习内容")
        self.message = self.message.strip()
        if self.scope == "global" and len(self.message) < 2:
            raise ValueError("请补充至少两个字的反馈说明")
        if self.feedback_type == "other" and len(self.message) < 2:
            raise ValueError("选择其他时请补充反馈说明")
        return self


class AskMeReply(ApiModel):
    answer: str = Field(default="", max_length=3000)


class AskMeDiscussionTurnCreate(ApiModel):
    session_id: str = Field(min_length=1, max_length=160)
    topic_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    answer: str = Field(min_length=1, max_length=3000)


class AskMeDiscussionAction(ApiModel):
    session_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    action: Literal["next_topic", "pause", "resume", "finish"]


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


class ChapterSkipCreate(ApiModel):
    reason: Literal["not_focus", "defer_unknown", "challenge_exit"]


class ChapterChallengeSectionSubmission(ApiModel):
    section_id: str = Field(min_length=1, max_length=160)
    quiz_set_id: str = Field(min_length=1, max_length=160)
    answers: list[list[int]] = Field(min_length=1, max_length=30)


class ChapterChallengeSubmit(ApiModel):
    sections: list[ChapterChallengeSectionSubmission] = Field(
        min_length=1, max_length=12
    )

    @model_validator(mode="after")
    def sections_are_unique(self):
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("章挑战不能重复提交同一小节")
        return self


class BookReplanCreate(ApiModel):
    feedback: str = Field(default="", max_length=3000)
    previous_proposal_id: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("feedback")
    @classmethod
    def normalize_feedback(cls, value: str):
        return value.strip()

    @model_validator(mode="after")
    def feedback_requires_proposal(self):
        if self.previous_proposal_id and not self.feedback:
            raise ValueError("针对目录重做时必须说明希望如何调整")
        return self
