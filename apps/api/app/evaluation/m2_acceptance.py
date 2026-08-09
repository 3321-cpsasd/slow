from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GateStatus = Literal["pass", "fail", "not_run"]


M2_HARD_GATES = {
    "M2-A1": "Mission、Learning Contract、正文、题目、观察与笔记版本链可重建",
    "M2-A2": "门禁、掌握与复习投影可从不可变事实重建且历史决策可解释",
    "M2-B1": "试点培养方案与课程大纲具有可审计来源、版本、适用范围和摘要",
    "M2-B2": "人工复核发布的课程基准被规划采用并冻结版本与目标覆盖",
    "M2-B3": "开放能力声明验证方式，选择题不替代代码、实践或开放作答证据",
    "M2-C1": "试点路径使用稳定 ConceptRevision、LearningObjective 与类型化关系",
    "M2-C2": "核心主张具有 SourceVersion、Claim、Binding 或阻断型 KnowledgeGap",
    "M2-D1": "生成读取受节点、边和跳数预算约束的 KnowledgeContextPack",
    "M2-D2": "GenerationRun 审计上下文版本、预算、实际子图与裁剪原因",
    "M2-E1": "真实模型完成同一依赖路径至少三个目标的发布、作答和证据闭环",
    "M2-E2": "基准外目标、无支持核心主张和版本错配在正式持久化前失败关闭",
}


class M2GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate_id: str = Field(alias="gateId")
    status: GateStatus
    evidence: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class M2AcceptanceEvidence(BaseModel):
    """Machine-decidable M2 v2 result; findings never override a hard gate."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["m2_acceptance_v2"] = Field(
        default="m2_acceptance_v2",
        alias="schemaVersion",
    )
    run_id: str = Field(alias="runId", min_length=1)
    code_revision: str = Field(alias="codeRevision", min_length=1)
    gates: list[M2GateResult]

    @model_validator(mode="after")
    def has_exactly_one_result_for_each_hard_gate(self):
        result_ids = [result.gate_id for result in self.gates]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("duplicate M2 hard gate result")
        unknown = sorted(set(result_ids) - set(M2_HARD_GATES))
        missing = sorted(set(M2_HARD_GATES) - set(result_ids))
        if unknown or missing:
            raise ValueError(f"M2 hard gate mismatch: unknown={unknown}, missing={missing}")
        for result in self.gates:
            if result.status == "pass" and not result.evidence:
                raise ValueError(f"passing gate {result.gate_id} requires evidence")
        return self

    @property
    def decision(self) -> Literal["PASS", "FAIL"]:
        return "PASS" if all(item.status == "pass" for item in self.gates) else "FAIL"

    @property
    def blocking_gate_ids(self) -> list[str]:
        return [item.gate_id for item in self.gates if item.status != "pass"]
