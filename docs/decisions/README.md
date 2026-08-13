# Slow 架构决策记录

本目录保存会约束多个模块、影响产品语义或数据可信边界的架构决策。它不是临时方案笔记。

## 权威顺序

发生冲突时按以下顺序处理：

1. `AGENTS.md` 中的不可破坏产品与数据可信约束；
2. `PRODUCT_DNA.md` 中的产品层级和教学粒度；
3. 状态为 `Accepted` 的 ADR；
4. 当前代码和测试。

代码与上位文档冲突时，表示迁移尚未完成，不能通过重新解释文档来让旧实现合理化。

## 状态

- `Proposed`：正在讨论，不能作为实现完成标准；
- `Accepted，等待实现`：决策已经确定，实现尚未满足全部验收条件；
- `Accepted，已实现`：实现和最低验收测试已经完成；
- `Superseded`：已被后续 ADR 明确取代；
- `Rejected`：保留历史原因，但不得实施。

## 变更规则

- 措辞澄清、链接修复和不改变语义的补充可以直接修改原 ADR；
- 改变权威来源、产品层级、状态机、持久化边界或安全边界时，必须新增 ADR；
- 新 ADR 必须注明被取代的决策，旧 ADR 状态改为 `Superseded`，不得静默覆盖历史决定；
- ADR 从“等待实现”改为“已实现”前，必须满足其中列出的最低验收测试；
- Changelog 应记录新增、接受、实现或取代重要 ADR 的事件。

## 当前决策

| ADR | 状态 | 主题 |
| --- | --- | --- |
| [ADR-0001](0001-lesson-generation-v2.md) | 部分被 ADR-0016 取代；发布边界继续有效 | Learning Contract、稳定绑定、失败关闭和原子发布 |
| [ADR-0002](0002-curriculum-planning-boundaries.md) | Accepted，核心切片已实现 | 分层课程规划、书籍目录激活、语义冻结和小节数量软约束 |
| [ADR-0003](0003-modular-monolith-boundaries.md) | Accepted，第一阶段已实现 | 应用模块化单体、写入权边界和渐进式门面迁移 |
| [ADR-0004](0004-curriculum-baseline-authority.md) | Accepted，首个发布纵向切片已通过 M2 | 真实课程基准、候选知识图、人工发布边界和目标覆盖门禁 |
| [ADR-0005](0005-m2-acceptance-v2.md) | Accepted，M2 十一项门禁已通过 | M2 五阶段可信链路薄切片、机器判定和里程碑边界 |
| [ADR-0006](0006-daily-mode.md) | Accepted | Daily Mode 的短期学习情境、活动连续性与证据边界 |
| [ADR-0007](0007-user-interface-expression-boundary.md) | Accepted，已实现首轮收敛 | 普通用户界面与内部治理、运行和审计机制的表达边界 |
| [ADR-0008](0008-section-continuity-and-recovery.md) | Accepted，第一阶段已实现 | 一节内容缓冲、统一重新准备入口和 Recovery Agent 权限边界 |
| [ADR-0009](0009-adaptive-lesson-composition.md) | Accepted，已实现 | 跨知识类型的动态正文编排、段落职责和案例可信边界 |
| [ADR-0011](0011-m3-pilot-readiness.md) | Accepted，实施中 | 模型故障透明恢复、五类错因、补救有效性、跨类型质量与偏好授权 |
| [ADR-0012](0012-on-demand-knowledge-universe-and-learner-memory.md) | Accepted，第一阶段实现中 | 按需知识宇宙、个人知识子网、三层学习记忆与可信知识段位 |
| [ADR-0013](0013-evidence-guided-reinforcement-agent.md) | Accepted，MVP 已实现 | 到期唤醒失败后的有界诊断、补强、重组与独立验证 |
| [ADR-0014](0014-purpose-aware-ai-gateway.md) | Accepted，第一阶段已实现 | 用途感知 AI 网关、模型池路由、评估独立性与准确性反馈复核边界 |
| [ADR-0015](0015-rank-settleable-learning-contracts.md) | Accepted，已实现 | 新小节段位可结算身份、系列内稳定复用与发布失败关闭 |
| [ADR-0016](0016-trusted-assessment-model-roles.md) | Accepted，已实现 | 全部正式选择题的出题、独立审题和答案盲判职责分离 |
| [ADR-0017](0017-independent-chapter-scope-review.md) | Accepted，已实现 | 独立模型通读整章，以最小编辑消除小节知识增量重复 |
| [ADR-0018](0018-m4-trustworthy-adaptive-learning.md) | Accepted，实现完成，等待真实模型验收 | M4 可信测评、口试职责、跨书适配和生产失败关闭的统一验收合同 |
| [ADR-0019](0019-learning-start-and-chapter-route-choices.md) | Accepted，MVP 已实现 | 系列启动兴趣选择、章级挑战与显式略过的路线/掌握分离 |
