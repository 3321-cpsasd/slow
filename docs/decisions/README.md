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
| [ADR-0012](0012-on-demand-knowledge-universe-and-learner-memory.md) | 部分被 ADR-0022 取代；知识宇宙、证据与记忆边界继续有效 | 按需知识宇宙、个人知识子网、三层学习记忆与可信知识段位 |
| [ADR-0013](0013-evidence-guided-reinforcement-agent.md) | Accepted，MVP 已实现 | 到期唤醒失败后的有界诊断、补强、重组与独立验证 |
| [ADR-0014](0014-purpose-aware-ai-gateway.md) | Accepted，第一阶段已实现 | 用途感知 AI 网关、模型池路由、评估独立性与准确性反馈复核边界 |
| [ADR-0015](0015-rank-settleable-learning-contracts.md) | 段位映射被 ADR-0022 取代；知识身份与发布门禁继续有效 | 新小节段位可结算身份、系列内稳定复用与发布失败关闭 |
| [ADR-0016](0016-trusted-assessment-model-roles.md) | Accepted，已实现 | 全部正式选择题的出题、独立审题和答案盲判职责分离 |
| [ADR-0017](0017-independent-chapter-scope-review.md) | Accepted，已实现 | 独立模型通读整章，以最小编辑消除小节知识增量重复 |
| [ADR-0018](0018-m4-trustworthy-adaptive-learning.md) | Accepted，实现完成，等待真实模型验收 | M4 可信测评、口试职责、跨书适配和生产失败关闭的统一验收合同 |
| [ADR-0019](0019-learning-start-and-chapter-route-choices.md) | Accepted，MVP 已实现 | 系列启动兴趣选择、章级挑战与显式略过的路线/掌握分离 |
| [ADR-0020](0020-audited-historical-rank-identity.md) | Accepted，已实现 | 历史学习证据的追加式段位身份决定、确定性画像重放与在线空结算防护 |
| [ADR-0021](0021-series-local-knowledge-identity-resolution.md) | Accepted，第一版薄切片已实现 | 系列内按需知识候选、追加式身份裁决、跨书复用与能力维度分离 |
| [ADR-0022](0022-capability-profile-and-cumulative-stages.md) | Accepted，章节综合能力规划、多节点能力子网、青铜至钻石正式任务与跨系列发布身份链路已实现 | 基于知识子网的稳定能力、四级累计阶段、三轴能力画像与复习证据入口 |

## Changelog

- 2026-08-15：接受 ADR-0022，明确以 Stable Capability 取代知识节点六级段位；ADR-0012、ADR-0015 的知识身份、证据与失败关闭边界继续有效。
- 2026-08-15：完成 ADR-0022 第一阶段影子链路：能力身份、四级量规、路线/目标绑定、独立证据资格、三轴累计投影和正文生成上下文接入；上层产品切换仍待后续阶段。
- 2026-08-16：完成 ADR-0022 白银口试纵向链路：契约内分离选择题与诊断目标，机制和边界两项强口试证据累计晋级白银，迁移口试保持诊断且不得制造钻石。
- 2026-08-16：完成 ADR-0022 黄金标准应用纵向链路：冻结未见构造任务、原始提交、逐项量规评定与模型家族独立性，只有已达白银且无辅助正式通过才累计晋级黄金；Demo 和不合格评定失败关闭。
- 2026-08-16：完成 ADR-0022 跨系列发布身份链路：经审核的概念、关系和能力子网可按精确语义跨系列复用；同一家族语义变化默认 unresolved，只有显式 supersedes 裁决才能形成稳定身份下的新 Revision。
- 2026-08-16：完成能力驱动复习选择第一切片：按 Capability 去重，到期能力唤醒优先于近期错题补强，错题降为次级信号；多题型 Stage Criterion 任务规划仍待后续。
- 2026-08-16：完成 ADR-0022 钻石正式迁移纵向链路：冻结陌生情境、知识重组与决策理由，独立评定且严格要求黄金前置；诊断迁移口试继续与正式钻石证据隔离。
- 2026-08-16：完成阶段感知复习任务规划：遗忘再激活按当前已获阶段选择任务且只影响 activation，有意识强化才读取首个缺失量规；未发布的高阶任务不会形成虚假晋级建议，高阶 Review Assignment 执行器仍待接入。
