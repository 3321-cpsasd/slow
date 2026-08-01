<p align="center">
  <img src="apps/web/public/slow-mark.svg" width="96" height="96" alt="Slow logo">
</p>

<h1 align="center">Slow · 知行书架</h1>

<p align="center">
  An AI-native personal learning bookshelf.<br>
  把学习目标变成可以真正学完的书。
</p>

<p align="center">
  <a href="#中文">中文</a> · <a href="#english">English</a>
</p>

---

## 中文

Slow 是一个 AI 原生个人学习应用。它把学习目标组织为可阅读、可验证、可持续推进的个性化教材，而不是只生成一份静态计划。

### 当前能力

- React + TypeScript 学习界面
- FastAPI + SQLAlchemy 服务端
- 个性化课程、正文与测验生成
- 服务端评分、渐进解锁与失败补救
- 与具体学习内容绑定的 Ask AI
- 可选的多轮 Ask Me 检查
- 持久化后台任务、恢复与幂等处理
- 本地开发身份和 OIDC 生产身份边界
- OpenAI 与 Anthropic 兼容供应商接口

### 快速开始

环境要求：Python 3.12+、Node.js 22+、pnpm 11+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
cd apps/web && pnpm install && cd ../..
cp .env.example .env
./start.sh
```

默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- OpenAPI：`http://127.0.0.1:8000/docs`

未配置外部模型时，开发环境使用明确标记的本地 Demo 数据。Demo 数据不能被当作真实 AI 内容或正式学习证据。

### 验证

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
cd apps/web && pnpm build
```

### 安全说明

- API Key 只存在服务端，不应进入浏览器、日志或 Git。
- 正式环境应使用 OIDC，并关闭本地开发身份与 Demo 模式。
- 解锁、评分和学习状态由服务端负责，不能信任前端状态。
- `.env`、运行时配置、数据库、附件与内部评测证据均被 Git 忽略。

---

## English

Slow is an AI-native personal learning application. It turns a learning goal into structured material that learners can study, verify, and continue over time instead of stopping at a static plan.

### Highlights

- React and TypeScript learning interface
- FastAPI and SQLAlchemy backend
- Personalized curriculum, lesson, and quiz generation
- Server-side grading, progressive unlocking, and remediation
- Context-bound Ask AI and optional multi-round Ask Me checks
- Durable background tasks with recovery and idempotency
- Local development identity and an OIDC production boundary
- OpenAI- and Anthropic-compatible provider interfaces

### Quick start

Requires Python 3.12+, Node.js 22+, and pnpm 11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
cd apps/web && pnpm install && cd ../..
cp .env.example .env
./start.sh
```

### Verification

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
cd apps/web && pnpm build
```

External model credentials are optional in development and must remain server-side. Production deployments should use OIDC and disable local/demo identity modes.
