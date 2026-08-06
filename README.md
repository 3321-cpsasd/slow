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

课程、生成、目录和学习证据共同遵守 [`用户 → 书架 → 系列（学习目标） → 书 → 章 → 节`](PRODUCT_DNA.md) 的领域契约；内容块只是节内结构，不是目录或解锁层级。正文与测验生成遵循 [ADR-0001](docs/decisions/0001-lesson-generation-v2.md)。

### 当前能力

- React + TypeScript 学习界面
- FastAPI + SQLAlchemy 服务端
- 个性化课程、正文与测验生成
- 服务端评分、渐进解锁与失败补救
- 与具体学习内容绑定的 Ask AI
- 可选的多轮 Ask Me 检查
- 持久化后台任务、恢复与幂等处理
- 邀请制账号密码登录、本地开发身份和可选 OIDC 身份边界
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
- 正式内测可使用管理员预创建的账号密码；生产环境必须关闭本地开发身份与 Demo 模式。
- 解锁、评分和学习状态由服务端负责，不能信任前端状态。
- `.env`、运行时配置、数据库、附件与内部评测证据均被 Git 忽略。

### 邀请制内测账号

生产内测使用 `APP_MODE=production` 与 `AUTH_MODE=password`。应用不提供公开注册；
首次部署并完成迁移后，由管理员在 API 容器中创建账号：

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
账号和 Argon2id 密码哈希随 SQLite 数据库持久化到 `/opt/slow/data`；明文密码只在
调用终端显示一次，不会进入密码托管文件。请立即复制并通过私密渠道发送给用户。

创建和重置命令默认生成随机密码；也可使用 `--prompt-password` 安全输入自定密码。
禁用账号或重置密码会撤销该用户的全部现有 Session。默认 Session 最长 7 天，
连续 24 小时未使用则过期。

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

### 许可证

本项目源代码依据 [GNU Affero General Public License v3.0](LICENSE)
发布，仅适用该版本（`AGPL-3.0-only`）。

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
