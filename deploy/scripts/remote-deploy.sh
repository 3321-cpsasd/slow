#!/bin/sh
set -eu

deploy_root=${DEPLOY_ROOT:-/opt/slow}
compose_file="$deploy_root/compose.prod.yml"
release_file="$deploy_root/.release"
release_env="$deploy_root/.release.env"

: "${APP_VERSION:?APP_VERSION is required}"
: "${IMAGE_NAME:?IMAGE_NAME is required}"
REGISTRY=${REGISTRY:-ghcr.io}

cd "$deploy_root"

if [ ! -f .env ]; then
  echo "Missing $deploy_root/.env" >&2
  exit 1
fi

previous_version=""
if [ -f "$release_file" ]; then
  previous_version=$(sed -n '1p' "$release_file")
fi

if docker compose --env-file "$release_env" -f "$compose_file" ps -q api 2>/dev/null | grep -q .; then
  docker compose --env-file "$release_env" -f "$compose_file" exec -T api \
    python -c "import datetime, pathlib, sqlite3; root=pathlib.Path('/data/backups'); root.mkdir(exist_ok=True); source=sqlite3.connect('/data/slow-v0.db'); target=sqlite3.connect(root / ('slow-' + datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ') + '.db')); source.backup(target); target.close(); source.close()"
fi

cat > "$release_env" <<EOF
REGISTRY=$REGISTRY
IMAGE_NAME=$IMAGE_NAME
APP_VERSION=$APP_VERSION
EOF

docker compose --env-file "$release_env" -f "$compose_file" pull
docker compose --env-file "$release_env" -f "$compose_file" up -d --remove-orphans

healthy=false
attempt=1
while [ "$attempt" -le 45 ]; do
  if curl --fail --silent --show-error http://127.0.0.1/api/health >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
  attempt=$((attempt + 1))
done

if [ "$healthy" != true ]; then
  docker compose --env-file "$release_env" -f "$compose_file" logs --tail=120
  if [ -n "$previous_version" ]; then
    cat > "$release_env" <<EOF
REGISTRY=$REGISTRY
IMAGE_NAME=$IMAGE_NAME
APP_VERSION=$previous_version
EOF
    docker compose --env-file "$release_env" -f "$compose_file" up -d --remove-orphans
    echo "Deployment failed; rolled back to $previous_version." >&2
  else
    echo "Initial deployment failed; no previous release is available." >&2
  fi
  exit 1
fi

printf '%s\n' "$APP_VERSION" > "$release_file"
find "$deploy_root/data/backups" -type f -name 'slow-*.db' -mtime +14 -delete
docker image prune -f >/dev/null
echo "Deployment $APP_VERSION is healthy."
