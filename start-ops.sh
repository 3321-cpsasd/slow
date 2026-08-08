#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ops_root="$project_root/apps/ops"
password_file="$project_root/data/ops-reporting.password"
node_bin=${NODE_BINARY:-}

if [ -z "$node_bin" ]; then
  node_bin=$(command -v node || true)
fi
if [ -z "$node_bin" ] && [ -x /Users/pix/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node ]; then
  node_bin=/Users/pix/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
fi
if [ -z "$node_bin" ]; then
  echo "未找到 Node.js 22+。" >&2
  exit 1
fi

if [ ! -f "$password_file" ]; then
  echo "缺少 $password_file；请先运行生产只读角色配置。" >&2
  exit 1
fi

"$ops_root/scripts/start-tunnel.sh" &
tunnel_pid=$!
cleanup() {
  kill "$tunnel_pid" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

attempt=1
while [ "$attempt" -le 60 ]; do
  if nc -z 127.0.0.1 "${OPS_DB_PORT:-15432}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
  attempt=$((attempt + 1))
done
if ! nc -z 127.0.0.1 "${OPS_DB_PORT:-15432}" >/dev/null 2>&1; then
  echo "SSH 数据库隧道未能启动。" >&2
  exit 1
fi

cd "$ops_root"
OPS_DB_PASSWORD_FILE="$password_file" "$node_bin" dist-server/server.js
