<p align="center">
  <img src="apps/web/public/slow-mark.svg" width="96" height="96" alt="Slow logo">
</p>

<h1 align="center">Slow · 知行书架</h1>

<p align="center">
  <strong>把学习目标变成可以真正学完的书。</strong><br>
  An AI-native personal learning system built around understanding, verification, and durable memory.
</p>

<p align="center">
  <a href="https://slow.net.cn">体验产品</a> ·
  <a href="https://3321-cpsasd.github.io/slow/">阅读文档</a> ·
  <a href="mailto:alpha@slow.net.cn">申请 Alpha 测试</a>
</p>

<p align="center">
  <a href="https://github.com/3321-cpsasd/slow/actions/workflows/ci.yml"><img src="https://github.com/3321-cpsasd/slow/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--only-4f7666" alt="AGPL-3.0-only"></a>
</p>

---

## Slow 是什么

Slow 是一个 AI 原生个人学习系统。它不会在收到主题后只交付一份“看起来完整”的学习计划，而是把学习目标组织成个性化教材，让学习者逐节阅读、提问、验证，并把真实学习结果带入后续内容。

产品围绕一个简单的判断展开：**AI 可以快速生成内容，但理解必须经过学习，掌握必须留下证据。**

因此，Slow 的核心交付不是一次性的回答，而是一条可以持续推进的学习闭环：

```text
提出学习目标
  → 形成有序教材
  → 逐节阅读与 Ask AI
  → 选择题验证
  ├─ 未通过：定位缺口 → 补救教学 → 同目标新题
  └─ 通过：记录学习证据 → 解锁下一节
       └─ 满分：可选 Ask Me（机制 → 边界 → 迁移）
```

## 与常见 AI 学习工具有什么不同

| 常见做法 | Slow 的选择 |
|---|---|
| 生成一份课程计划后结束 | 交付可逐节学习、验证和完成的教材 |
| 每次对话都从零开始 | 将测验、答疑和口试沉淀为学习证据 |
| 用“读过”代替“掌握” | 通过后才解锁下一节，满分后再开放迁移口试 |
| 所有人获得相同讲解 | 后续内容参考已有证据，减少重复并补足薄弱关联 |
| 把模型输出直接当正式内容 | 候选内容先经过结构与契约校验，再原子发布 |
| 前端自己决定进度 | 评分、解锁和学习状态全部由服务端裁决 |

## 产品模型

Slow 的目录不是普通文件夹，而是学习语义的一部分：

```text
用户 → 书架 → 系列（学习目标） → 书 → 章 → 节
                                      └→ 内容块（节内教学结构）
```

- **书架**承载一个长期学科或领域。
- **系列**对应一个已确认的学习目标，并组织完成目标所需的有序书籍。
- **书**围绕一个完整、可命名的学习主题展开。
- **章**聚合一组相关知识点，通常对应约一天的学习。
- **节**锚定一个核心知识点，是最小学习与验证单元，典型投入为 15–20 分钟。
- **内容块**用于定义、机制、例子、边界或练习，不构成新的目录和解锁层级。

完整定义和不可破坏的粒度规则见 [PRODUCT_DNA.md](PRODUCT_DNA.md)。面向学习者的解释见[官方文档：核心概念](https://3321-cpsasd.github.io/slow/textbook-not-plan)。

## 当前学习体验

- 根据目标、角色、经验和期望深度生成系列、书与章节目录。
- 阅读带有明确内容模式与来源边界的个性化正文。
- 在具体小节和段落上下文中使用 Ask AI，不打断阅读主流程。
- 每节结束进行服务端评分的选择题验证；失败后提供补救教学与新题。
- 满分后可进入 Ask Me，依次检验机制、边界和迁移能力。
- 将测验和 Ask Me 结果写入掌握画像，用于后续生成与重规划。
- 通过后生成可编辑的个人笔记，保留易错点、答疑结论和未解决问题。
- 使用版本化内容、持久后台任务和失败关闭边界保护学习链路。

Slow 目前处于邀请制 Alpha 阶段。你可以访问 [slow.net.cn](https://slow.net.cn)，或发送邮件至 [alpha@slow.net.cn](mailto:alpha@slow.net.cn)，简单说明希望学习的主题以申请测试资格。

## 文档导航

### 学习者与首次访问者

- [官方使用指南](https://3321-cpsasd.github.io/slow/)：了解产品、开始第一次学习并查看核心概念。
- [为什么是教材，而不是计划](https://3321-cpsasd.github.io/slow/textbook-not-plan)：理解 Slow 的基本产品判断。
- [学习证据与个性化](https://3321-cpsasd.github.io/slow/evidence-and-personalization)：了解后续内容如何适应真实学习结果。
- [AI、Demo 与可信边界](https://3321-cpsasd.github.io/slow/ai-content)：了解系统能够和不能保证什么。

### 开发者与贡献者

- [产品底层基因](PRODUCT_DNA.md)：产品层级、学习粒度与证据边界。
- [架构决策记录](docs/decisions/README.md)：正文生成、课程规划、UI 表达和连续学习等决策。
- [正文生成与原子发布](docs/decisions/0001-lesson-generation-v2.md)：Learning Contract、内容版本和发布门禁。
- [课程规划边界](docs/decisions/0002-curriculum-planning-boundaries.md)：系列、书、章、节如何形成与重规划。
- [模块化单体边界](docs/decisions/0003-modular-monolith-boundaries.md)：应用、领域模块和基础设施的依赖规则。
- [界面表达边界](docs/decisions/0007-user-interface-expression-boundary.md)：内部机制如何转换为用户可理解的状态与行动。
- [试点运营](deploy/PILOT_OPERATIONS.md)：邀请制测试和运营流程。

## 本地运行

### 环境要求

- Python 3.12+
- Node.js 22+
- pnpm 11+

### 启动应用

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
cd apps/web && pnpm install && cd ../..
cp .env.example .env
./start.sh
```

默认服务地址：

| 服务 | 地址 |
|---|---|
| Web 应用 | `http://127.0.0.1:5173` |
| 应用内文档 | `http://127.0.0.1:5173/docs` |
| API | `http://127.0.0.1:8000` |
| OpenAPI | `http://127.0.0.1:8000/docs` |

开发环境未配置外部模型时，只允许使用明确标记的 Demo 数据。Demo 内容不得伪装成真实 AI 结果或正式学习证据。模型密钥只能保存在服务端环境变量中。

### 验证改动

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
cd apps/web && pnpm build
```

验证 GitHub Pages 独立文档构建：

```bash
cd apps/web && pnpm build:pages
```

更多数据库、部署和专项验证命令请查看对应文档，不在项目首页复制生产操作流程。

## 技术架构

Slow 当前保持为适合产品验证期的模块化单体：

```text
React + TypeScript + Vite
            │ REST / JSON / NDJSON
            ▼
FastAPI + Pydantic
  ├─ 身份、权限与用户作用域
  ├─ 学习、书架、答疑与产物模块
  ├─ 评分、解锁与确定性领域规则
  ├─ AI 能力端口与结构化校验
  └─ 持久后台任务、版本与审计
            │
            ▼
SQLAlchemy + Alembic + SQLite / PostgreSQL
```

AI 负责规划、生成、辅导与评价建议；服务端规则负责身份、归属、评分、完成、解锁和事务裁决。模型输出始终是候选结果，不能绕过服务端校验直接进入正式学习链路。

## 仓库结构

```text
apps/
  api/          FastAPI 服务、领域规则、AI 端口和数据库
  web/          React 学习应用与官方 Docs
  ops/          本机只读运营工具
deploy/         部署配置与试点运营文档
docs/           系统说明、质量门禁与架构决策
.github/        CI、发布与 Docs Pages 工作流
```

## 参与项目

当前阶段优先验证真实学习闭环、生成质量、学习证据和用户留存。提交改动前，请先确认它没有破坏以下原则：

- 教材层级保持为“用户 → 书架 → 系列 → 书 → 章 → 节”。
- 支撑知识不能静默变成新的考核目标。
- 失败的生成候选不能成为用户可读的正式内容。
- 解锁、评分和掌握度不能信任浏览器状态。
- Demo、降级和未核验内容必须明确标识。
- 用户界面表达任务和影响，不直接暴露内部实现术语。

建议从 [PRODUCT_DNA.md](PRODUCT_DNA.md) 和 [架构决策索引](docs/decisions/README.md) 开始阅读，再通过 Issue 或 Pull Request 参与讨论。

## English

Slow is an AI-native personal learning system that turns a learning goal into structured material a learner can actually study, verify, and finish. It treats understanding as a process and mastery as evidence—not as a side effect of content generation.

The core loop is: **goal → personalized textbook → section-level study → assessment → remediation or unlock → durable learning evidence**. Later content uses that evidence to avoid unnecessary repetition and provide scaffolding where it is needed.

- Product: [slow.net.cn](https://slow.net.cn)
- User documentation: [3321-cpsasd.github.io/slow](https://3321-cpsasd.github.io/slow/)
- Alpha access: [alpha@slow.net.cn](mailto:alpha@slow.net.cn)
- Product contract: [PRODUCT_DNA.md](PRODUCT_DNA.md)
- Architecture decisions: [docs/decisions](docs/decisions/README.md)

Local development requires Python 3.12+, Node.js 22+, and pnpm 11+. Follow the commands in [Local development](#本地运行) to start the application.

## License

Copyright © Slow contributors. Licensed under the [GNU Affero General Public License v3.0](LICENSE), version 3 only (`AGPL-3.0-only`).
