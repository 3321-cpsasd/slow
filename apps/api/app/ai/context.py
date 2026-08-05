import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ContextOperation = Literal[
    "plan",
    "book_replan",
    "chapter",
    "lesson_content",
    "lesson_quiz",
    "remediation",
    "source_repair",
    "review_quiz",
    "ask_ai",
    "ask_me",
    "learning_note",
]


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LearnerContext(ContextModel):
    profile_version: int = Field(default=0, alias="profileVersion")
    profession: str = ""
    stage: str = ""
    purpose: str = ""
    domains: list[str] = Field(default_factory=list)
    experience: str = ""
    weekly_minutes: int = Field(default=0, alias="weeklyMinutes")
    target_date: str = Field(default="", alias="targetDate")
    preferences: dict[str, Any] = Field(default_factory=dict)
    plan_role: str = Field(default="", alias="planRole")
    plan_experience: str = Field(default="", alias="planExperience")
    provenance: str = "confirmed_profile"

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class MissionContext(ContextModel):
    version_id: str = Field(default="", alias="versionId")
    version: int = 0
    why: str = ""
    target_capabilities: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="targetCapabilities",
    )
    constraints: dict[str, Any] = Field(default_factory=dict)
    out_of_scope: list[str] = Field(default_factory=list, alias="outOfScope")
    assumptions: list[str] = Field(default_factory=list)
    learner_context: dict[str, Any] = Field(
        default_factory=dict,
        alias="learnerContext",
    )
    status: str = ""


class CurriculumContext(ContextModel):
    shelf: dict[str, Any] = Field(default_factory=dict)
    series: dict[str, Any] = Field(default_factory=dict)
    book: dict[str, Any] = Field(default_factory=dict)
    chapter: dict[str, Any] = Field(default_factory=dict)
    section: dict[str, Any] = Field(default_factory=dict)
    neighboring_chapters: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="neighboringChapters",
    )
    neighboring_sections: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="neighboringSections",
    )


class LearningStateContext(ContextModel):
    relevant_memory: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="relevantMemory",
    )
    evidence_watermark: int = Field(default=0, alias="evidenceWatermark")
    attempt: dict[str, Any] = Field(default_factory=dict)
    feedback: dict[str, Any] = Field(default_factory=dict)


class GenerationPolicy(ContextModel):
    version: str
    required_categories: list[str] = Field(alias="requiredCategories")
    allowed_uses: list[str] = Field(alias="allowedUses")
    forbidden_uses: list[str] = Field(alias="forbiddenUses")
    depth_policy: dict[str, Any] = Field(default_factory=dict, alias="depthPolicy")


class ContextLineage(ContextModel):
    profile_version: int = Field(default=0, alias="profileVersion")
    mission_version_id: str = Field(default="", alias="missionVersionId")
    contract_version_id: str = Field(default="", alias="contractVersionId")
    evidence_watermark: int = Field(default=0, alias="evidenceWatermark")
    policy_version: str = Field(alias="policyVersion")


class GenerationContextPack(ContextModel):
    operation: ContextOperation
    learner: LearnerContext
    mission: MissionContext | None = None
    learning_contract: dict[str, Any] = Field(
        default_factory=dict,
        alias="learningContract",
    )
    curriculum: CurriculumContext
    learning_state: LearningStateContext = Field(alias="learningState")
    interaction: dict[str, Any] = Field(default_factory=dict)
    policy: GenerationPolicy
    lineage: ContextLineage

    def payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def manifest(self) -> dict[str, Any]:
        payload = self.payload()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        included = [
            "learner",
            "curriculum",
            "learningState",
            "policy",
        ]
        if self.mission:
            included.append("mission")
        if self.learning_contract:
            included.append("learningContract")
        if self.interaction:
            included.append("interaction")
        return {
            "operation": self.operation,
            "contextHash": hashlib.sha256(encoded).hexdigest(),
            "includedCategories": included,
            "lineage": self.lineage.model_dump(by_alias=True, mode="json"),
        }


DEPTH_POLICIES: dict[str, dict[str, Any]] = {
    "overview": {
        "label": "简单了解",
        "scope": "只覆盖建立方向感所需的核心对象、用途与关键边界",
        "requiredDimensions": ["recognition", "mechanism"],
        "practiceMode": "guided",
        "retentionRequired": False,
    },
    "deep": {
        "label": "深度学习",
        "scope": "覆盖机制、相近概念比较、边界判断与典型应用",
        "requiredDimensions": ["mechanism", "boundary", "application"],
        "practiceMode": "applied",
        "retentionRequired": False,
    },
    "mastery": {
        "label": "掌握路径",
        "scope": "覆盖机制、边界、迁移、开放实践与延迟保持",
        "requiredDimensions": ["mechanism", "boundary", "transfer", "retention"],
        "practiceMode": "independent",
        "retentionRequired": True,
    },
}


CONTEXT_POLICIES: dict[ContextOperation, dict[str, Any]] = {
    "plan": {
        "required": ["learner", "curriculum", "learningState"],
        "allowed": [
            "use confirmed profile and relevant evidence to choose scope and starting point",
            "treat the submitted role and purpose as plan-specific intent",
        ],
        "forbidden": [
            "treat self-report as verified mastery",
            "silently infer a different learning mission",
        ],
    },
    "book_replan": {
        "required": ["learner", "mission", "curriculum", "learningState"],
        "allowed": ["change only unstarted future chapters"],
        "forbidden": ["rewrite started content", "weaken verified success criteria"],
    },
    "chapter": {
        "required": ["learner", "mission", "curriculum", "learningState"],
        "allowed": ["adapt explanations and section sequence to the learner"],
        "forbidden": ["change the adopted mission", "repeat verified targets without need"],
    },
    "lesson_content": {
        "required": ["learner", "mission", "curriculum", "learningState", "contract"],
        "allowed": [
            "adapt examples, terminology and explanation depth",
            "use explicit presentation preferences to rank pedagogically valid formats",
        ],
        "forbidden": [
            "change assessment targets",
            "claim mastery",
            "invent learner experience",
            "treat a presentation preference as evidence of learning effectiveness",
        ],
    },
    "lesson_quiz": {
        "required": ["mission", "curriculum", "contract"],
        "allowed": ["use learner context only for a familiar scenario"],
        "forbidden": [
            "change correctness or required targets based on profile",
            "lower the verification gate",
        ],
    },
    "remediation": {
        "required": ["learner", "mission", "curriculum", "learningState", "contract"],
        "allowed": ["diagnose from the exact submitted answer and scoring result"],
        "forbidden": ["teach unrelated branches", "treat dialogue as mastery evidence"],
    },
    "source_repair": {
        "required": ["mission", "curriculum", "contract"],
        "allowed": ["patch failed sources and dependent blocks only"],
        "forbidden": ["rewrite unrelated blocks", "change the adopted learner context"],
    },
    "review_quiz": {
        "required": ["curriculum", "learningState", "contract"],
        "allowed": ["generate an equivalent, substantively new item"],
        "forbidden": ["lower difficulty", "reuse prior item family"],
    },
    "ask_ai": {
        "required": ["learner", "mission", "curriculum", "interaction", "contract"],
        "allowed": ["adapt the explanation to the learner and anchored paragraph"],
        "forbidden": ["answer an active assessment", "read unrelated private content"],
    },
    "ask_me": {
        "required": ["mission", "curriculum", "interaction", "contract"],
        "allowed": ["use the learner role to choose a transfer scenario"],
        "forbidden": ["teach during evaluation", "change the scoring rubric by profile"],
    },
    "learning_note": {
        "required": ["mission", "curriculum", "learningState", "contract"],
        "allowed": ["prioritize evidence relevant to the learner mission"],
        "forbidden": ["turn probability into a learner-authored conclusion"],
    },
}


def policy_for(operation: ContextOperation, depth: str) -> GenerationPolicy:
    definition = CONTEXT_POLICIES[operation]
    return GenerationPolicy(
        version=f"{operation}_context_v1",
        requiredCategories=definition["required"],
        allowedUses=definition["allowed"],
        forbiddenUses=definition["forbidden"],
        depthPolicy=DEPTH_POLICIES.get(depth, DEPTH_POLICIES["deep"]),
    )
