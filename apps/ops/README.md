# Slow 本地运营瞭望台

只读展示生产用户运营指标。服务仅监听 `127.0.0.1`，通过 SSH 隧道访问东京 PostgreSQL 的 `ops_reporting.user_metrics_v1` 安全屏障视图。

## 首次配置

1. 在生产主机运行 `deploy/scripts/configure-ops-reporting.sh`，创建只读角色和视图。
2. 将 `/opt/slow/data/ops-reporting.password` 安全复制到本机 `data/ops-reporting.password`，权限保持为 `0600`。
3. 在 `apps/ops` 安装依赖并构建：`pnpm install && pnpm build`。

## 启动

从仓库根目录运行：

```bash
./start-ops.sh
```

然后访问 `http://127.0.0.1:4174`。关闭进程会同时关闭 SSH 隧道。

可选环境变量：

- `OPS_REMOTE_HOST`：默认 `root@8.216.45.77`
- `OPS_DB_PORT`：本地隧道端口，默认 `15432`
- `OPS_PORT`：本地页面端口，默认 `4174`

模型单价只保存在当前页面内存中，不会写回生产数据库或浏览器存储。
