# Slow · 知行书架

[中文](#中文) · [English](#english)

Slow is an AI-native personal learning bookshelf that turns a learning goal into structured books users can study, verify, and retain one section at a time.

Slow 是一个 AI 原生个人学习书架：把学习目标转化为可以逐节学习、验证并持续沉淀记忆的个性化教材。

---

## 中文

### 项目简介

Slow 不交付一份静态学习计划，而是构建完整的学习闭环：

```text
用户 → 书架 → 书 → 章 → 节
                    ↓
             阅读 → 答疑 → 测验 → 解锁 → 记忆
```

- 每本书预计 3–15 天完成，每章约对应一天。
- 每章包含 3–5 节，每节约 20 分钟，只解决一个清晰问题。
- 每节结束必须通过服务端评分的选择题，及格后才解锁下一节。
- 满分后可进入可选的 Ask Me 口试，依次验证机制、边界和迁移能力。
- Ask AI 绑定具体小节和内容块，不打断或污染主阅读流程。
- 测验和口试证据会沉淀为可追溯的学习记忆，用于后续内容个性化。

### 当前能力

- 书架、系列、书、章、节的完整内容层级
- 基于角色、经验、目标和深度生成个性化学习路径
- 章节与小节按需生成，支持来源和内容版本记录
- 服务端测验评分、失败补救、新题重试和渐进解锁
- 段落级 Ask AI、多轮 Ask Me 和可编辑个人笔记
- 章末实践、书末综合项目及附件提交
- 不可变学习证据与可重建掌握度投影
- 本地 Demo Adapter、真实 OpenAI Responses API 和独立评测 Runner

### 技术架构

Slow 采用前后端分离的模块化单体架构。浏览器负责展示和交互，FastAPI 是 AI 调用、业务规则、内容版本、评分、解锁和学习证据的唯一可信边界。

| 层 | 技术 |
|---|---|
| Web | React、TypeScript、Vite |
| API | Python 3.12+、FastAPI、Pydantic 2 |
| 数据 | SQLAlchemy 2、Alembic、SQLite |
| AI | OpenAI Python SDK、Responses API |
| 验证 | pytest、TypeScript 编译、Vite 构建、黑盒 Agent 评测 |

```text
apps/
├── api/                 FastAPI 应用、领域规则、AI Adapter、迁移与测试
└── web/                 React 学习界面
docs/
├── PRODUCT_BOUNDARY.md  产品边界
└── ARCHITECTURE.md      架构与数据规则
reports/evaluations/     评测历史与证据
```

### 快速开始

环境要求：

- Python 3.12+
- Node.js 与 pnpm
- macOS/Linux shell
- 可选：OpenAI API Key（仅保存在服务端）

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
cd apps/web && pnpm install && cd ../..
```

创建本地配置：

```bash
cp .env.example .env
```

在 `.env` 中按需填写：

```dotenv
OPENAI_API_KEY=your_server_side_key
OPENAI_MODEL=gpt-5.6-terra
API_HOST=127.0.0.1
API_PORT=8000
WEB_ORIGIN=http://127.0.0.1:5173
```

启动前后端：

```bash
./start.sh
```

| 服务 | 地址 |
|---|---|
| Web | `http://127.0.0.1:5173` |
| API | `http://127.0.0.1:8000` |
| OpenAPI | `http://127.0.0.1:8000/docs` |

没有配置 `OPENAI_API_KEY` 时，系统使用机器可识别的 `local-demo-v1` 内容演示学习状态机。Demo 数据不能作为真实 AI 内容或正式评测证据。

### 验证

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
cd apps/web && pnpm build
```

运行黑盒学习者与独立评审：

```bash
PYTHONPATH=apps/api .venv/bin/python -m app.evaluation.runner
PYTHONPATH=apps/api .venv/bin/python -m app.evaluation.runner --real
```

本地模式只验证评测框架；`--real` 会调用已配置模型、核验来源并产生实际 API 费用。报告写入 `reports/evaluations/`。

### README 与变更日志检查

仓库提供版本化 Git hooks。首次 clone 后执行：

```bash
./scripts/install-git-hooks.sh
```

之后每次 `git pull` 完成 merge 或 rebase 时，hook 都会检查本次更新是否涉及产品代码、配置、迁移或脚本，并提示是否需要同步维护 `README.md` 和 `CHANGELOG.md`。检查只提醒、不阻止 pull；是否需要修改仍由开发者根据实际语义判断。

手动检查两个 Git 引用之间的变化：

```bash
./scripts/check-doc-updates.sh <旧引用> <新引用>
```

### 数据可信原则

- API Key 仅存在服务端，不返回浏览器或写入日志。
- AI 结构化结果必须通过 JSON Schema 与服务端校验。
- Mock、Demo、测试和降级数据必须显式标记并与真实证据隔离。
- 解锁和掌握度只由服务端计算，前端状态不作为权威来源。
- 内容、来源、生成、测验和重要状态变化需要版本化且可追溯。

更完整的产品与技术约束见 [`docs/PRODUCT_BOUNDARY.md`](docs/PRODUCT_BOUNDARY.md) 和 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## English

### Overview

Slow does not produce a static study plan. It provides an end-to-end learning loop:

```text
User → Shelf → Book → Chapter → Section
                          ↓
             Read → Ask → Quiz → Unlock → Remember
```

- A book is designed for roughly 3–15 days, with each chapter representing about one day.
- A chapter contains 3–5 focused sections; each section takes about 20 minutes.
- Learners must pass a server-graded multiple-choice quiz before the next section unlocks.
- A perfect score reveals the optional Ask Me oral checkpoint for mechanism, boundary, and transfer.
- Ask AI is anchored to a specific section and content block, keeping Q&A outside the reading flow.
- Quiz and oral-checkpoint evidence feeds a traceable learning-memory profile for future personalization.

### Current capabilities

- Complete shelf, series, book, chapter, and section hierarchy
- Personalized learning paths based on role, experience, purpose, and target depth
- On-demand chapter and section generation with sources and content versions
- Server-side grading, remediation, fresh quiz retries, and progressive unlocking
- Block-level Ask AI, multi-round Ask Me, and editable personal notes
- Chapter practices, book capstones, and attachment submission
- Immutable learning evidence and rebuildable mastery projections
- Local Demo Adapter, real OpenAI Responses API integration, and an independent evaluation runner

### Architecture

Slow is a front-end/back-end separated modular monolith. The browser owns presentation and interaction; FastAPI is the sole trusted boundary for AI calls, domain rules, content versions, grading, unlocking, and learning evidence.

| Layer | Stack |
|---|---|
| Web | React, TypeScript, Vite |
| API | Python 3.12+, FastAPI, Pydantic 2 |
| Data | SQLAlchemy 2, Alembic, SQLite |
| AI | OpenAI Python SDK, Responses API |
| Verification | pytest, TypeScript checks, Vite builds, black-box agent evaluation |

```text
apps/
├── api/                 FastAPI app, domain rules, AI adapters, migrations, tests
└── web/                 React learning interface
docs/
├── PRODUCT_BOUNDARY.md  Product boundaries
└── ARCHITECTURE.md      Architecture and data rules
reports/evaluations/     Evaluation history and evidence
```

### Quick start

Prerequisites:

- Python 3.12+
- Node.js and pnpm
- A macOS/Linux shell
- Optional: an OpenAI API key, stored only on the server

Install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
cd apps/web && pnpm install && cd ../..
```

Create local configuration:

```bash
cp .env.example .env
```

Configure `.env` as needed:

```dotenv
OPENAI_API_KEY=your_server_side_key
OPENAI_MODEL=gpt-5.6-terra
API_HOST=127.0.0.1
API_PORT=8000
WEB_ORIGIN=http://127.0.0.1:5173
```

Start both services:

```bash
./start.sh
```

| Service | URL |
|---|---|
| Web | `http://127.0.0.1:5173` |
| API | `http://127.0.0.1:8000` |
| OpenAPI | `http://127.0.0.1:8000/docs` |

Without `OPENAI_API_KEY`, Slow uses machine-identifiable `local-demo-v1` content to demonstrate the learning state machine. Demo data is never treated as real AI content or formal evaluation evidence.

### Verification

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
cd apps/web && pnpm build
```

Run the black-box learner and independent reviewer:

```bash
PYTHONPATH=apps/api .venv/bin/python -m app.evaluation.runner
PYTHONPATH=apps/api .venv/bin/python -m app.evaluation.runner --real
```

Local mode validates the evaluation framework only. `--real` calls the configured model, verifies source reachability, and incurs actual API cost. Reports are written to `reports/evaluations/`.

### README and changelog checks

The repository includes versioned Git hooks. After the first clone, run:

```bash
./scripts/install-git-hooks.sh
```

After every `git pull` merge or rebase, the hook checks whether product code, configuration, migrations, or scripts changed and reminds the developer to review `README.md` and `CHANGELOG.md`. The check is advisory and never blocks a pull; maintainers decide whether a documentation change is semantically required.

To inspect a range manually:

```bash
./scripts/check-doc-updates.sh <old-ref> <new-ref>
```

### Data trust principles

- API keys remain server-side and are never returned to the browser or written to logs.
- Structured AI output must pass JSON Schema and server-side validation.
- Mock, demo, test, and fallback data is explicitly labeled and isolated from real evidence.
- Unlocking and mastery are calculated only on the server; client state is never authoritative.
- Content, sources, generations, quizzes, and important state changes are versioned and traceable.

See [`docs/PRODUCT_BOUNDARY.md`](docs/PRODUCT_BOUNDARY.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full product and technical constraints.
