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

previous_version=""
if [ -f "$release_file" ]; then
  previous_version=$(sed -n '1p' "$release_file")
fi

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

compose pull
compose up -d --remove-orphans db api web

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
  compose logs --tail=120
  if [ -n "$previous_version" ]; then
    cat > "$release_env" <<EOF
REGISTRY=$REGISTRY
IMAGE_NAME=$IMAGE_NAME
APP_VERSION=$previous_version
WEB_ORIGIN=$WEB_ORIGIN
EOF
    compose up -d --remove-orphans db api web
    echo "Deployment failed; rolled back to $previous_version." >&2
  else
    echo "Initial deployment failed; no previous release is available." >&2
  fi
  exit 1
fi

printf '%s\n' "$APP_VERSION" > "$release_file"
find "$deploy_root/data/backups" -type f -name 'slow-*.dump' -mtime +14 -delete
docker image prune -f >/dev/null
echo "Deployment $APP_VERSION is healthy."
