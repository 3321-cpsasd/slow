#!/bin/sh
set -eu

deploy_root=${DEPLOY_ROOT:-/opt/slow}
compose_file="$deploy_root/compose.prod.yml"
https_compose_override="$deploy_root/compose.https.yml"
release_env="$deploy_root/.release.env"

for required_file in "$compose_file" "$https_compose_override" "$release_env"; do
  if [ ! -f "$required_file" ]; then
    echo "Missing required production file: $required_file" >&2
    exit 1
  fi
done

cd "$deploy_root"
docker compose \
  --env-file "$release_env" \
  -f "$compose_file" \
  -f "$https_compose_override" \
  exec -T api python manage_users.py create-demo "$@"
