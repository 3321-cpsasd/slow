#!/bin/sh
set -eu

deploy_root=${DEPLOY_ROOT:-/opt/slow}
runtime_env="$deploy_root/.env"
release_env="$deploy_root/.release.env"
compose_file="$deploy_root/compose.prod.yml"
https_override="$deploy_root/compose.https.yml"
view_sql=${OPS_REPORTING_SQL:-"$deploy_root/data/ops-reporting-v1.sql"}
password_file=${OPS_REPORTING_PASSWORD_FILE:-"$deploy_root/data/ops-reporting.password"}

for required in "$runtime_env" "$release_env" "$compose_file" "$https_override" "$view_sql"; do
  if [ ! -f "$required" ]; then
    echo "Missing required file: $required" >&2
    exit 1
  fi
done

if [ ! -f "$password_file" ]; then
  umask 077
  openssl rand -base64 36 > "$password_file"
fi
chmod 0600 "$password_file"

compose() {
  docker compose --env-file "$runtime_env" --env-file "$release_env" \
    -f "$compose_file" -f "$https_override" "$@"
}

db_container=$(compose ps -q db)
if [ -z "$db_container" ]; then
  echo "Production PostgreSQL container is not running." >&2
  exit 1
fi

docker cp "$password_file" "$db_container:/tmp/slow-ops-reporting.password"
cleanup() {
  docker exec "$db_container" rm -f /tmp/slow-ops-reporting.password >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

{
  printf '%s\n' '\set ON_ERROR_STOP on'
  printf '%s\n' '\set ops_password `cat /tmp/slow-ops-reporting.password`'
  printf '%s\n' "SELECT 'CREATE ROLE slow_ops_ro' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'slow_ops_ro') \\gexec"
  printf '%s\n' "ALTER ROLE slow_ops_ro WITH LOGIN PASSWORD :'ops_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION CONNECTION LIMIT 2;"
  printf '%s\n' "ALTER ROLE slow_ops_ro SET default_transaction_read_only = on;"
  printf '%s\n' "ALTER ROLE slow_ops_ro SET statement_timeout = '2s';"
  cat "$view_sql"
  printf '%s\n' 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM slow_ops_ro;'
  printf '%s\n' 'REVOKE ALL ON SCHEMA public FROM slow_ops_ro;'
  printf '%s\n' 'GRANT USAGE ON SCHEMA ops_reporting TO slow_ops_ro;'
  printf '%s\n' 'GRANT SELECT ON ops_reporting.user_metrics_v1 TO slow_ops_ro;'
} | compose exec -T db sh -c 'exec psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'

cleanup
trap - EXIT INT TERM
echo "Read-only operations reporting role and view are ready."
