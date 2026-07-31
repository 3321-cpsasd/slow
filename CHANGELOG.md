# Changelog · 变更日志

All notable changes to Slow are recorded here. This project follows the structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Slow 的重要变更记录在这里。本项目采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式。

## [Unreleased] · 未发布

### Added · 新增

- Added a competition-review guide and a canonical evaluation index that distinguishes current evidence, historical failures, and open gates.
- 新增参赛评审指南与权威评测索引，明确区分当前证据、历史失败与尚未关闭的门禁。
- Added repository-hosted product screenshots for the bookshelf and personalized learning-goal flow.
- 新增仓库内产品截图，展示个人书架与个性化学习目标流程。
- Added durable first-lesson preloading: a new learning plan now queues its first chapter outline, lesson content, sources, and quiz immediately, while the UI tracks the task and opens the prepared section automatically.
- 新增持久化首节预生成：学习计划创建后立即排队生成首章小节、第一节正文、来源与测验；前端持续跟踪任务，并在完成后自动打开首节。
- Added a provider-neutral structured-output repair harness: schema failures now trigger bounded repair-only calls with field-level validation feedback, fail closed after the retry budget, and persist privacy-safe attempt metadata and output digests in generation traces.
- 新增供应商无关的结构化输出修复 Harness：Schema 失败后执行有上限的纯修复调用，携带字段级校验反馈；耗尽预算后明确失败，并在生成轨迹中持久化不含原文的尝试元数据与输出摘要。
- Re-ran the previously failed real-model section with the new harness; content, quiz, stable block IDs, and reachable sources all passed without duplicate persisted versions.
- 使用新 Harness 重新运行此前失败的真实模型小节；正文、题集、稳定内容块 ID 与来源可达性均通过，且未产生重复持久化版本。
- Added OIDC authorization-code login with PKCE, state/nonce and signed ID-token validation, revocable server-side sessions, CSRF protection, and fail-closed production authentication.
- 新增带 PKCE、state/nonce 与签名 ID Token 校验的 OIDC 授权码登录、可撤销服务端 Session、CSRF 防护，以及正式环境身份失败关闭。
- Added actor/subject principals, user-scoped aggregate authorization, composite learning-run/user integrity constraints, and two-user isolation tests.
- 新增执行者/受益用户 Principal、用户级聚合授权、学习运行/用户复合一致性约束与双用户隔离测试。
- Added fenced worker leases with owner/token/expiry/heartbeat checks, immutable artifact submission facts, cross-device resume positions, and a projection rebuild tool.
- 新增带 owner/token/过期/心跳校验的 Worker fencing、不可变成果提交事实、跨设备恢复位置与投影重建工具。
- Added GitHub Actions CI/CD for API tests, Web builds, immutable GHCR images, and health-checked Alibaba Cloud ECS deployment with SQLite backups and image rollback.
- 新增 GitHub Actions CI/CD：覆盖 API 测试、Web 构建、不可变 GHCR 镜像，以及带 SQLite 备份、健康检查和镜像回滚的阿里云 ECS 部署。
- Added a durable post-quiz task queue for non-blocking note generation, remediation, equivalent-quiz generation, and automatic next-section preloading, with recovery, idempotency, status polling, and safe retry.
- 新增测验后持久化任务队列：评分不再等待模型，支持笔记、补救教学、等价题与下一节自动预加载，并提供恢复、幂等、状态轮询和安全重试。
- Increased the external model request timeout to 300 seconds after real compatible-provider evidence showed valid structured generations can exceed the previous 120-second limit; scoring remains non-blocking through durable tasks.
- 根据真实兼容供应商证据，将外部模型请求超时从 120 秒调整为 300 秒；有效结构化生成可能超过旧上限，评分仍通过持久化任务保持无阻塞。
- Added server-only persistence for validated AI settings, with private file permissions, restart recovery, redacted browser status, and fail-closed validation.
- 新增通过验证的 AI 设置服务端持久化：使用私有文件权限、支持重启恢复、浏览器仅返回脱敏状态，并在配置损坏时拒绝静默降级。
- Added per-book soft deletion with confirmation, audit retention, automatic next-book unlocking, and empty-series cleanup.
- 新增单本书软删除：包含二次确认、审计保留、后续书籍自动解锁与空系列清理。
- Added run-scoped learning facts, explicit progress projections, atomic quiz progression, recoverable note tasks, fixed-query library read models, and immutable migration metadata.
- 新增按学习运行隔离的事实与进度投影、原子测验推进、可恢复笔记任务、固定查询数读模型，以及不可变迁移元数据。
- Added explicit OpenAI-compatible and Anthropic-compatible provider protocols, including Anthropic Messages streaming and schema validation.
- 新增显式 OpenAI 兼容与 Anthropic 兼容供应商协议，支持 Anthropic Messages 流式答疑和结构化结果校验。
- Added a complete bilingual Chinese/English README covering the product loop, architecture, setup, verification, data-trust rules, and contributor workflow.
- 新增完整的中英文双语 README，覆盖产品闭环、技术架构、启动验证、数据可信原则和协作流程。
- Added version-controlled Git hooks that review README and changelog coverage after pull merges and rebases.
- 新增可纳入版本控制的 Git hooks，在 pull 合并或 rebase 后检查 README 与变更日志覆盖情况。

### Changed · 调整

- Made the Web launcher portable, removed workstation paths from project context and historical/future evaluation reports, aligned the example model with the server default, and made production deployment explicitly opt-in.
- 提升 Web 启动脚本可移植性，移除项目上下文及历史/后续评测报告中的本机路径，统一示例模型与服务端默认值，并将生产部署改为显式开启。
- Declared the supported Node.js and pnpm versions so local and CI builds use the same runtime contract.
- 声明受支持的 Node.js 与 pnpm 版本，使本地和 CI 构建遵循同一运行时约定。
- Made identity, worker-fencing, resume-position, and artifact-submission migrations recover safely when a prior ORM startup pre-created future tables.
- 让身份、Worker fencing、跨设备阅读位置与成果提交迁移可从 ORM 提前建表的中间状态安全恢复。
- Added database-backed generation leases so refreshes, multiple tabs, or
  multiple workers cannot start duplicate chapter or section model calls; stale
  leases expire for safe recovery.
- 新增数据库生成租约：刷新、多标签页或多进程不会重复启动同一章/节的模型调用；中断后的过期租约可安全恢复。
- Prevented repeated chapter-generation submissions while a request or initial preload is already active, avoiding duplicate concurrent model calls from rapid clicks.
- 在章节生成请求或首节预生成进行中锁定重复提交，避免连续点击触发并发模型调用。
- Bound the local project to `https://github.com/3321-cpsasd/slow.git` as the `origin` remote.
- 将本地项目的 `origin` 远程仓库绑定为 `https://github.com/3321-cpsasd/slow.git`。
