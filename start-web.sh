#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
pnpm_bin=/Users/pix/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm
node_dir=/Users/pix/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin

if [ ! -x "$pnpm_bin" ]; then
  pnpm_bin=pnpm
fi

cd "$project_dir/apps/web"
exec env PATH="$node_dir:/usr/local/bin:/usr/bin:/bin" CI=true "$pnpm_bin" dev
