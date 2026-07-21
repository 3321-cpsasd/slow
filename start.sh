#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

cd "$project_dir"
./start-api.sh &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM
./start-web.sh
