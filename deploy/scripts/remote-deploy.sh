#!/bin/sh
set -eu

deploy_root=${DEPLOY_ROOT:-/opt/slow}
compose_file="$deploy_root/compose.prod.yml"
https_compose_override="$deploy_root/compose.https.yml"
release_file="$deploy_root/.release"
release_env="$deploy_root/.release.env"
runtime_env="$deploy_root/.env"
database_authority_file="$deploy_root/data/database-authority"

: "${APP_VERSION:?APP_VERSION is required}"
: "${IMAGE_NAME:?IMAGE_NAME is required}"
: "${WEB_ORIGIN:?WEB_ORIGIN is required}"
REGISTRY=${REGISTRY:-ghcr.io}

cd "$deploy_root"

if [ ! -f .env ]; then
  echo "Missing $deploy_root/.env" >&2
  exit 1
fi
if [ ! -f "$https_compose_override" ]; then
  echo "Missing production HTTPS compose override: $https_compose_override" >&2
  exit 1
fi
if [ ! -f "$database_authority_file" ] || \
   [ "$(sed -n '1p' "$database_authority_file")" != "postgresql" ]; then
  echo "PostgreSQL is not the recorded database authority; run the documented SQLite cutover first." >&2
  exit 1
fi

compose() {
  docker compose --env-file "$runtime_env" --env-file "$release_env" \
    -f "$compose_file" -f "$https_compose_override" "$@"
}

log_failed_startup() {
  echo "Deployment startup diagnostics:" >&2
  compose ps -a >&2 || true
  compose logs --no-color --tail=200 db api web >&2 || true
}

previous_release_env=""
if [ -f "$release_env" ]; then
  previous_release_env=$(mktemp "$deploy_root/.release.env.previous.XXXXXX")
  cp "$release_env" "$previous_release_env"
fi

cleanup_previous_release_env() {
  if [ -n "$previous_release_env" ]; then
    rm -f "$previous_release_env"
  fi
}
trap cleanup_previous_release_env EXIT HUP INT TERM

restore_previous_release() {
  if [ -z "$previous_release_env" ]; then
    return 1
  fi
  cp "$previous_release_env" "$release_env"
  (
    set -a
    . "$release_env"
    set +a
    compose up -d --remove-orphans --force-recreate db api web
  )
}

if [ -f "$release_env" ] && compose ps -q db 2>/dev/null | grep -q .; then
  backup_timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup_file="$deploy_root/data/backups/slow-$backup_timestamp.dump"
  backup_next="$backup_file.next"
  compose exec -T db sh -c \
    'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
    > "$backup_next"
  if [ ! -s "$backup_next" ] || ! compose exec -T db pg_restore --list < "$backup_next" >/dev/null; then
    rm -f "$backup_next"
    echo "PostgreSQL backup validation failed; deployment stopped." >&2
    exit 1
  fi
  mv "$backup_next" "$backup_file"
fi

cat > "$release_env" <<EOF
REGISTRY=$REGISTRY
IMAGE_NAME=$IMAGE_NAME
APP_VERSION=$APP_VERSION
WEB_ORIGIN=$WEB_ORIGIN
EOF

if ! compose pull; then
  if [ -n "$previous_release_env" ]; then
    cp "$previous_release_env" "$release_env"
  fi
  echo "Deployment image pull failed; restored the previous release metadata." >&2
  exit 1
fi

if ! compose up -d --remove-orphans db api web; then
  log_failed_startup
  if restore_previous_release; then
    echo "Deployment failed while starting containers; restored the previous release." >&2
  else
    echo "Initial deployment failed while starting containers; no previous release exists." >&2
  fi
  exit 1
fi

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
  log_failed_startup
  if restore_previous_release; then
    echo "Deployment failed health checks; restored the previous release." >&2
  else
    echo "Initial deployment failed; no previous release is available." >&2
  fi
  exit 1
fi

printf '%s\n' "$APP_VERSION" > "$release_file"
find "$deploy_root/data/backups" -type f -name 'slow-*.dump' -mtime +14 -delete
docker image prune -f >/dev/null
echo "Deployment $APP_VERSION is healthy."
