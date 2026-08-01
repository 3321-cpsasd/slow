#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin="$project_dir/.venv/bin/python"
alembic_bin="$project_dir/.venv/bin/alembic"
uvicorn_bin="$project_dir/.venv/bin/uvicorn"

if [ ! -x "$python_bin" ]; then
  echo "缺少 Python 环境，请先执行：python3 -m venv .venv && .venv/bin/pip install -r apps/api/requirements.txt" >&2
  exit 1
fi

cd "$project_dir/apps/api"
PYTHONPATH=. "$alembic_bin" -c alembic.ini upgrade head
exec env PYTHONPATH=. AUTH_MODE="${AUTH_MODE:-local}" "$uvicorn_bin" app.main:app --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8000}" --reload
