#!/bin/sh
set -eu

remote_host=${OPS_REMOTE_HOST:-root@8.216.45.77}
remote_root=${OPS_REMOTE_ROOT:-/opt/slow}
local_port=${OPS_DB_PORT:-15432}

db_ip=$(ssh -o BatchMode=yes "$remote_host" "cd '$remote_root' && container_id=\$(docker compose --env-file .env --env-file .release.env -f compose.prod.yml -f compose.https.yml ps -q db) && docker inspect \"\$container_id\" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'")
case "$db_ip" in
  ''|*[!0-9.]*)
    echo "无法解析生产 PostgreSQL 容器地址。" >&2
    exit 1
    ;;
esac

echo "正在建立本地 127.0.0.1:$local_port 的只读数据库隧道…"
exec ssh \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -N \
  -L "127.0.0.1:$local_port:$db_ip:5432" \
  "$remote_host"
