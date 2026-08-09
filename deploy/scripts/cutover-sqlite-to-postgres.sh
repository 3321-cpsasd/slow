#!/bin/sh
set -eu

deploy_root=${DEPLOY_ROOT:-/opt/slow}
compose_file="$deploy_root/compose.prod.yml"
rollback_override="$deploy_root/compose.sqlite-rollback.yml"
https_override="$deploy_root/compose.https.yml"
demo_override="$deploy_root/compose.demo.yml"
release_env="$deploy_root/.release.env"
runtime_env="$deploy_root/.env"
authority_file="$deploy_root/data/database-authority"
sqlite_file="$deploy_root/data/slow-v0.db"
backup_root="$deploy_root/data/backups"
DEPLOY_MODE=${DEPLOY_MODE:-production}

case "$DEPLOY_MODE" in
  demo)
    mode_override="$demo_override"
    ;;
  production)
    mode_override="$https_override"
    ;;
  *)
    echo "DEPLOY_MODE must be 'demo' or 'production'." >&2
    exit 1
    ;;
esac

for required in "$compose_file" "$rollback_override" "$mode_override" "$release_env" "$runtime_env" "$sqlite_file"; do
  if [ ! -f "$required" ]; then
    echo "Missing cutover prerequisite: $required" >&2
    exit 1
  fi
done
if [ -f "$authority_file" ]; then
  current_authority=$(sed -n '1p' "$authority_file")
  case "$current_authority" in
    sqlite)
      ;;
    postgresql)
      echo "PostgreSQL is already authoritative; refusing to repeat the cutover." >&2
      exit 1
      ;;
    *)
      echo "Unknown database authority '$current_authority'; refusing the cutover." >&2
      exit 1
      ;;
  esac
fi

compose_postgres() {
  docker compose --env-file "$runtime_env" --env-file "$release_env" \
    -f "$compose_file" -f "$mode_override" "$@"
}

compose_sqlite() {
  docker compose --env-file "$runtime_env" --env-file "$release_env" \
    -f "$compose_file" -f "$rollback_override" -f "$mode_override" "$@"
}

restore_sqlite_service() {
  compose_sqlite up -d --force-recreate api web
}

mkdir -p "$backup_root"
backup_timestamp=$(date -u +%Y%m%dT%H%M%SZ)
sqlite_backup_name="slow-pre-postgresql-$backup_timestamp.db"
sqlite_backup="$backup_root/$sqlite_backup_name"

# Stop every public writer before taking the final authoritative snapshot.
compose_sqlite stop web api
if ! compose_sqlite run --rm --no-deps --entrypoint python api -c \
  'import os,sqlite3,sys; source=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); target=sqlite3.connect(sys.argv[2]); source.backup(target); result=target.execute("PRAGMA integrity_check").fetchone()[0]; target.close(); source.close(); assert result == "ok" and os.path.getsize(sys.argv[2]) > 0' \
  /data/slow-v0.db "/data/backups/$sqlite_backup_name"; then
  restore_sqlite_service
  echo "SQLite backup failed; restored the SQLite service." >&2
  exit 1
fi
if [ ! -s "$sqlite_backup" ]; then
  restore_sqlite_service
  echo "SQLite backup validation failed; restored the SQLite service." >&2
  exit 1
fi

restore_pre_cutover_snapshot() {
  if ! cp "$sqlite_backup" "$sqlite_file"; then
    echo "Failed to restore the pre-cutover SQLite snapshot; public services remain stopped." >&2
    return 1
  fi
  restore_sqlite_service
}

# The long-lived Demo host may be several Alembic revisions behind the release.
# Upgrade the stopped SQLite authority with the target image before importing it.
# Any later pre-authority failure restores the validated, pre-upgrade snapshot.
if ! compose_sqlite run --rm --no-deps api true; then
  restore_pre_cutover_snapshot
  echo "SQLite schema upgrade failed; restored the pre-cutover snapshot." >&2
  exit 1
fi

if ! compose_postgres up -d db; then
  restore_pre_cutover_snapshot
  echo "PostgreSQL failed to start; restored the pre-cutover SQLite snapshot." >&2
  exit 1
fi

healthy=false
attempt=1
while [ "$attempt" -le 45 ]; do
  if compose_postgres exec -T db sh -c \
    'pg_isready --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
    >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
  attempt=$((attempt + 1))
done
if [ "$healthy" != true ]; then
  restore_pre_cutover_snapshot
  echo "PostgreSQL did not become healthy; restored the pre-cutover SQLite snapshot." >&2
  exit 1
fi

# The API entrypoint upgrades the empty PostgreSQL schema before the importer
# runs. The importer itself is transactional and refuses a non-empty target.
if ! compose_postgres run --rm --no-deps api \
  python migrate_sqlite_to_postgres.py /data/slow-v0.db; then
  restore_pre_cutover_snapshot
  echo "SQLite import failed; restored the pre-cutover SQLite snapshot." >&2
  exit 1
fi
if ! compose_postgres run --rm --no-deps api \
  python migrate_sqlite_to_postgres.py /data/slow-v0.db --verify-only; then
  restore_pre_cutover_snapshot
  echo "PostgreSQL verification failed; restored the pre-cutover SQLite snapshot." >&2
  exit 1
fi

# Start only the private API first. The public web container remains stopped,
# so no PostgreSQL learner writes can race the final authority decision.
if ! compose_postgres up -d --force-recreate api; then
  restore_pre_cutover_snapshot
  echo "PostgreSQL API failed to start; restored the pre-cutover SQLite snapshot." >&2
  exit 1
fi
healthy=false
attempt=1
while [ "$attempt" -le 45 ]; do
  api_container=$(compose_postgres ps -q api)
  if [ -n "$api_container" ] && \
     [ "$(docker inspect "$api_container" --format '{{.State.Health.Status}}')" = "healthy" ]; then
    healthy=true
    break
  fi
  sleep 2
  attempt=$((attempt + 1))
done
if [ "$healthy" != true ]; then
  compose_postgres stop api
  restore_pre_cutover_snapshot
  echo "PostgreSQL API health check failed; restored the pre-cutover SQLite snapshot." >&2
  exit 1
fi

authority_next="$authority_file.next"
printf '%s\n' postgresql > "$authority_next"
chmod 600 "$authority_next"
mv "$authority_next" "$authority_file"

if ! compose_postgres up -d --force-recreate web; then
  echo "PostgreSQL is authoritative and API is healthy, but web failed to start; do not roll back SQLite after new writes." >&2
  exit 1
fi

echo "$DEPLOY_MODE PostgreSQL cutover completed; preserved SQLite backup: $sqlite_backup"
