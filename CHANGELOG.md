# Changelog · 变更日志

All notable changes to Slow are recorded here. This project follows the structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Slow 的重要变更记录在这里。本项目采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式。

## [Unreleased] · 未发布

### Added · 新增

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

- Bound the local project to `https://github.com/3321-cpsasd/slow.git` as the `origin` remote.
- 将本地项目的 `origin` 远程仓库绑定为 `https://github.com/3321-cpsasd/slow.git`。
