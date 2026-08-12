<p align="center">
  <img src="apps/web/public/slow-mark.svg" width="96" height="96" alt="Slow logo">
</p>

<h1 align="center">Slow · 知行书架</h1>

<p align="center">
  An AI-native personal learning bookshelf.<br>
  把学习目标变成可以真正学完的书。
</p>

<p align="center">
  <a href="https://slow.net.cn">访问产品 / Live site</a> ·
  <a href="mailto:alpha@slow.net.cn">申请 Alpha 测试 / Request access</a> ·
  <a href="#中文">中文</a> · <a href="#english">English</a>
</p>

---

## 中文

Slow 是一个 AI 原生个人学习应用。它把学习目标组织为可阅读、可验证、可持续推进的个性化教材，而不是只生成一份静态计划。

课程、生成、目录和学习证据共同遵守 [`用户 → 书架 → 系列（学习目标） → 书 → 章 → 节`](PRODUCT_DNA.md) 的领域契约；内容块只是节内结构，不是目录或解锁层级。正文与测验生成遵循 [ADR-0001](docs/decisions/0001-lesson-generation-v2.md)。

### 申请 Alpha 测试

Slow 目前采用邀请制测试。如果你希望体验产品，请发送邮件至
[alpha@slow.net.cn](mailto:alpha@slow.net.cn)，简单说明你想学习的主题；我们会在审核后回复注册邀请码。

### 当前能力

- React + TypeScript 学习界面
- FastAPI + SQLAlchemy 服务端
- 个性化课程、正文与测验生成
- 服务端评分、渐进解锁与失败补救
- 与具体学习内容绑定的 Ask AI
- 可选的多轮 Ask Me 检查
- 持久化后台任务、恢复与幂等处理
- 邀请制账号密码登录、本地开发身份和可选 OIDC 身份边界
- 版本化隐私同意、可审计的退出与数据删除申请
- 本机运营快照导出，不开放无权限边界的公网管理接口
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

只读运营数据服务见 [`apps/ops/README.md`](apps/ops/README.md)。它运行在运营者本机，通过 SSH 隧道读取生产 PostgreSQL 的受限视图，不占用 ECS 常驻应用资源。

未配置外部模型时，开发环境使用明确标记的本地 Demo 数据。Demo 数据不能被当作真实 AI 内容或正式学习证据。

### 验证

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
cd apps/web && pnpm build
```

生产数据库兼容性使用显式的一次性 PostgreSQL 测试实例验证：

```bash
docker compose -f deploy/compose.postgres.test.yml down --remove-orphans
docker compose -f deploy/compose.postgres.test.yml up -d --wait
POSTGRES_TEST_DATABASE_URL=postgresql+psycopg://slow_test:slow_test_only@127.0.0.1:55432/slow_test \
  PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests/test_postgresql_support.py
docker compose -f deploy/compose.postgres.test.yml down --remove-orphans
```

真实课程基准候选包必须先通过 Schema、引用闭合和显式缺口校验；校验不会写入数据库：

```bash
PYTHONPATH=apps/api .venv/bin/python apps/api/import_curriculum_baseline.py \
  apps/api/curriculum_baselines/pku_cs_programming_practice_2025_v1.json \
  --validate-only
```

候选内容与人工审核决定分别版本化。审核清单必须冻结候选摘要、逐一覆盖来源、候选关系和显式缺口，并明确区分课程范围、知识发布及能力证据门禁：

```bash
PYTHONPATH=apps/api .venv/bin/python apps/api/import_curriculum_baseline.py \
  apps/api/curriculum_baselines/pku_cs_programming_practice_2025_v1.json \
  --review apps/api/curriculum_baselines/pku_cs_programming_practice_2025_v1_review_20260809.json \
  --validate-only
```

去掉 `--validate-only` 只导入候选和审核记录；再显式增加 `--publish` 才会尝试发布。课程范围仍有阻断项、来源未复核或审核清单不闭合时，发布失败且不会进入正式规划。知识事实和开放能力证据继续走独立门禁，不能因为课程基准已发布而自动升级。权威边界见 [ADR-0004](docs/decisions/0004-curriculum-baseline-authority.md)。

测试数据库使用公开的测试凭据和 `tmpfs`，不得与生产 Compose 合并。

### 安全说明

- API Key 只存在服务端，不应进入浏览器、日志或 Git。
- 正式内测可使用管理员预创建的账号密码；生产环境必须关闭本地开发身份与 Demo 模式。
- 解锁、评分和学习状态由服务端负责，不能信任前端状态。
- `.env`、运行时配置、数据库、附件与内部评测证据均被 Git 忽略。

### 邀请制内测账号

生产内测使用 `APP_MODE=production` 与 `AUTH_MODE=password`。默认不开放注册；
首次部署并完成迁移后，可继续由管理员在 API 容器中创建账号：

```bash
./create-demo-user.sh
./create-demo-user.sh --name '张三'
docker compose --env-file .release.env -f compose.prod.yml -f compose.https.yml exec api python manage_users.py create zhangsan --name '张三'
docker compose --env-file .release.env -f compose.prod.yml -f compose.https.yml exec api python manage_users.py disable zhangsan
docker compose --env-file .release.env -f compose.prod.yml -f compose.https.yml exec api python manage_users.py enable zhangsan
docker compose --env-file .release.env -f compose.prod.yml -f compose.https.yml exec api python manage_users.py reset-password zhangsan
```

`create-demo-user.sh` 必须在 ECS 的 `/opt/slow` 下调用。它会使用生产 HTTPS
Compose 配置自动创建 `slow-demo` 加五位随机数的账号，并生成一个 24 位强密码。
账号和 Argon2id 密码哈希随 PostgreSQL 数据库持久化；明文密码只在
调用终端显示一次，不会进入密码托管文件。请立即复制并通过私密渠道发送给用户。

创建和重置命令默认生成随机密码；也可使用 `--prompt-password` 安全输入自定密码。
禁用账号或重置密码会撤销该用户的全部现有 Session。默认 Session 最长 7 天，
连续 24 小时未使用则过期。

需要开放无邮箱 Alpha 注册时，在生产环境设置：

```bash
REGISTRATION_MODE=alpha
ALPHA_REGISTRATION_CODE=请使用独立高熵访问码
ALPHA_REGISTRATION_DAILY_LIMIT=100
```

新用户使用访问码创建用户名和密码，随后会收到仅展示一次的恢复码。恢复码
是无邮箱账号的自助重置凭证；服务端只保存其哈希。恢复成功后旧密码、旧恢复码
和全部现有 Session 都会失效，并生成新的恢复码。已登录用户验证当前密码后，也可以
在“账号与数据”中生成新的恢复码；生成后旧恢复码立即失效。`REGISTRATION_MODE=closed`
可随时停止新注册，不影响已有账号登录和恢复。`open` 模式不要求访问码，当前
Alpha 阶段不建议启用。

上线前如确实需要管理员重复查看分发密码，可在**非生产环境**显式设置
`PASSWORD_ESCROW_ENABLED=true`。创建或重置后的密码会写入独立的 `0600` 文件，
然后可执行：

```bash
PYTHONPATH=apps/api .venv/bin/python apps/api/manage_users.py show-password zhangsan
```

正式上线前先关闭该环境变量，再清除托管文件：

```bash
PYTHONPATH=apps/api .venv/bin/python apps/api/manage_users.py purge-passwords --confirm
```

`APP_MODE=production` 检测到密码托管开启时会拒绝启动。清理服务器文件后，还应按
备份保留策略删除可能含有该文件的历史备份。密码重置只撤销身份 Session，不删除或
重建用户，因此书架、学习进度、测验记录和掌握画像均保持不变。

内测运营者应遵循 [`deploy/PILOT_OPERATIONS.md`](deploy/PILOT_OPERATIONS.md)。生产环境会在学习画像和业务接口前要求当前版本的隐私与试点同意；运营台账快照只允许在 API 容器内导出：

```bash
python operations_report.py --include-identifiers
```

输出包含账号状态和学习漏斗指标，不包含密码、Session、API Key、学习正文、问答或笔记内容。

### 许可证

本项目源代码依据 [GNU Affero General Public License v3.0](LICENSE)
发布，仅适用该版本（`AGPL-3.0-only`）。

---

## English

Slow is an AI-native personal learning application. It turns a learning goal into structured material that learners can study, verify, and continue over time instead of stopping at a static plan.

### Request Alpha access

Slow is currently invite-only. To try the product, email
[alpha@slow.net.cn](mailto:alpha@slow.net.cn) with a short note about what you want to learn. We will review the request and reply with a registration invite code.

### Highlights

- React and TypeScript learning interface
- FastAPI and SQLAlchemy backend
- Personalized curriculum, lesson, and quiz generation
- Server-side grading, progressive unlocking, and remediation
- Context-bound Ask AI and optional multi-round Ask Me checks
- Durable background tasks with recovery and idempotency
- Invitation-only password accounts, local development identity, and optional OIDC
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

Production beta deployments can use `APP_MODE=production` with
`AUTH_MODE=password`. Accounts are created only by the server-side
`manage_users.py` command; there is no public registration endpoint.

External model credentials are optional in development and must remain server-side. Production deployments can use invitation-only password accounts or OIDC, and must disable local/demo identity modes.

### License

This project's source code is licensed under the
[GNU Affero General Public License v3.0](LICENSE), version 3 only
(`AGPL-3.0-only`).
