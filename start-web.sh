#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! command -v node >/dev/null 2>&1; then
  echo "缺少 Node.js，请先安装 Node.js 22+。" >&2
  exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
  echo "缺少 pnpm，请先安装 pnpm 11+。" >&2
  exit 1
fi

cd "$project_dir/apps/web"
exec pnpm dev
