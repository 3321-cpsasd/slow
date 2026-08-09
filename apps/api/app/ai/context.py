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
    baseline: dict[str, Any] = Field(default_factory=dict)


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
    curriculum_baseline_version_id: str = Field(
        default="",
        alias="curriculumBaselineVersionId",
    )
    knowledge_graph_release_id: str = Field(
        default="",
        alias="knowledgeGraphReleaseId",
    )
    knowledge_graph_release_version: int = Field(
        default=0,
        alias="knowledgeGraphReleaseVersion",
    )
    evidence_watermark: int = Field(default=0, alias="evidenceWatermark")
    policy_version: str = Field(alias="policyVersion")


class KnowledgeContextBudget(ContextModel):
    max_nodes: int = Field(alias="maxNodes", ge=1)
    max_edges: int = Field(alias="maxEdges", ge=0)
    max_hops: int = Field(alias="maxHops", ge=0)


class KnowledgeContextPack(ContextModel):
    """Immutable, bounded knowledge subgraph supplied to one generation task."""

    schema_version: str = Field(alias="schemaVersion")
    status: Literal["ready", "not_applicable"]
    reason: str = ""
    release_id: str = Field(default="", alias="releaseId")
    release_version: int = Field(default=0, alias="releaseVersion")
    baseline_version_id: str = Field(default="", alias="baselineVersionId")
    retrieval_rule_version: str = Field(alias="retrievalRuleVersion")
    budget: KnowledgeContextBudget
    seed_concept_revision_ids: list[str] = Field(
        default_factory=list,
        alias="seedConceptRevisionIds",
    )
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    truncation: dict[str, Any] = Field(default_factory=dict)
    context_hash: str = Field(default="", alias="contextHash")

    def audit_manifest(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "reason": self.reason,
            "releaseId": self.release_id,
            "releaseVersion": self.release_version,
            "baselineVersionId": self.baseline_version_id,
            "retrievalRuleVersion": self.retrieval_rule_version,
            "budget": self.budget.model_dump(by_alias=True, mode="json"),
            "seedConceptRevisionIds": list(self.seed_concept_revision_ids),
            "nodeRevisionIds": [
                str(item.get("conceptRevisionId", "")) for item in self.nodes
            ],
            "relationVersionIds": [
                str(item.get("relationVersionId", "")) for item in self.edges
            ],
            "claimVersionIds": [
                str(item.get("claimVersionId", "")) for item in self.claims
            ],
            "actual": {
                "nodeCount": len(self.nodes),
                "edgeCount": len(self.edges),
                "claimCount": len(self.claims),
            },
            "actualSubgraph": {
                "nodes": [
                    {
                        "conceptRevisionId": item.get("conceptRevisionId", ""),
                        "distance": item.get("distance", 0),
                        "role": item.get("role", ""),
                    }
                    for item in self.nodes
                ],
                "edges": [
                    {
                        "relationVersionId": item.get("relationVersionId", ""),
                        "fromConceptRevisionId": item.get(
                            "fromConceptRevisionId", ""
                        ),
                        "toConceptRevisionId": item.get("toConceptRevisionId", ""),
                        "relationType": item.get("relationType", ""),
                    }
                    for item in self.edges
                ],
                "claimVersionIds": [
                    str(item.get("claimVersionId", "")) for item in self.claims
                ],
            },
            "truncation": self.truncation,
            "contextHash": self.context_hash,
        }


class GenerationContextPack(ContextModel):
    operation: ContextOperation
    learner: LearnerContext
    mission: MissionContext | None = None
    learning_contract: dict[str, Any] = Field(
        default_factory=dict,
        alias="learningContract",
    )
    curriculum: CurriculumContext
    knowledge_context: KnowledgeContextPack = Field(alias="knowledgeContext")
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
            "knowledgeContext",
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
        "version": "plan_context_v2",
        "required": ["learner", "curriculum", "learningState"],
        "allowed": [
            "use confirmed profile and relevant evidence to choose scope and starting point",
            "treat the submitted role and purpose as plan-specific intent",
            "create one goal-bound series whose ordered books each own a coherent theme",
            "design every chapter as a cluster of related knowledge points",
        ],
        "forbidden": [
            "treat self-report as verified mastery",
            "silently infer a different learning mission",
            "use a book as a wrapper or alias for one chapter",
            "use one atomic 15-20 minute knowledge point as a chapter",
        ],
    },
    "book_replan": {
        "version": "book_replan_context_v2",
        "required": ["learner", "mission", "curriculum", "learningState"],
        "allowed": [
            "change only unstarted future chapters",
            "keep every future chapter as a cluster of related knowledge points",
        ],
        "forbidden": [
            "rewrite started content",
            "weaken verified success criteria",
            "replace a chapter with one atomic knowledge point",
        ],
    },
    "chapter": {
        "version": "chapter_context_v2",
        "required": ["learner", "mission", "curriculum", "learningState"],
        "allowed": [
            "adapt explanations and section sequence to the learner",
            "give each section one focal knowledge point while preserving necessary concept relations",
        ],
        "forbidden": [
            "change the adopted mission",
            "repeat verified targets without need",
            "turn generic exposition stages into navigation sections",
            "create a sub-section navigation level below the knowledge-point section",
            "treat the focal knowledge point as isolated from required prerequisites and relations",
        ],
    },
    "lesson_content": {
        "version": "lesson_content_context_v2",
        "required": ["learner", "mission", "curriculum", "learningState", "contract"],
        "allowed": [
            "adapt examples, terminology and explanation depth",
            "use explicit presentation preferences to rank pedagogically valid formats",
            "expand weak prerequisites and related concepts that are necessary to understand the focal point",
            "compress related concepts already supported by qualified learning evidence",
        ],
        "forbidden": [
            "change assessment targets",
            "claim mastery",
            "invent learner experience",
            "treat a presentation preference as evidence of learning effectiveness",
            "turn a supporting concept into an undeclared assessment target",
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
        version=definition.get("version", f"{operation}_context_v1"),
        requiredCategories=definition["required"],
        allowedUses=definition["allowed"],
        forbiddenUses=definition["forbidden"],
        depthPolicy=DEPTH_POLICIES.get(depth, DEPTH_POLICIES["deep"]),
    )
