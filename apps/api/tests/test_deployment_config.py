from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_login_rate_limit_uses_path_without_query_string():
    nginx_config = (PROJECT_ROOT / "deploy/nginx/default.conf").read_text(
        encoding="utf-8"
    )

    assert "map $uri $slow_login_limit_key" in nginx_config
    assert "map $request_uri $slow_login_limit_key" not in nginx_config
    assert "~^/api/auth/(password|local)/login$ $binary_remote_addr;" in nginx_config


def test_demo_user_script_targets_https_production_compose():
    script = (PROJECT_ROOT / "deploy/scripts/create-demo-user.sh").read_text(
        encoding="utf-8"
    )

    assert 'deploy_root=${DEPLOY_ROOT:-/opt/slow}' in script
    assert '-f "$compose_file"' in script
    assert '-f "$https_compose_override"' in script
    assert "exec -T api python manage_users.py create-demo" in script


def test_production_compose_uses_private_postgresql_authority():
    compose = (PROJECT_ROOT / "deploy/compose.prod.yml").read_text(
        encoding="utf-8"
    )

    assert "image: postgres:17.6-alpine" in compose
    assert "postgresql+psycopg://" in compose
    assert "sqlite+pysqlite" not in compose
    assert "pg_isready" in compose
    assert "./data/postgres:/var/lib/postgresql/data" in compose
    assert "5432:5432" not in compose


def test_deployment_backups_are_postgresql_and_cutover_is_fail_closed():
    for name in ("remote-build-deploy.sh", "remote-deploy.sh"):
        script = (PROJECT_ROOT / "deploy/scripts" / name).read_text(
            encoding="utf-8"
        )
        assert "database-authority" in script
        if name == "remote-build-deploy.sh":
            assert 'case "$database_authority" in' in script
            assert "Unknown database authority" in script
            assert 'if [ "$DEPLOY_MODE" != "demo" ]' in script
        else:
            assert '!= "postgresql"' in script
        assert "pg_dump" in script
        assert "pg_restore --list" in script
        assert "sqlite3" not in script
        assert '--env-file "$runtime_env" --env-file "$release_env"' in script


def test_cutover_stops_public_writes_and_verifies_before_authority_switch():
    script = (
        PROJECT_ROOT / "deploy/scripts/cutover-sqlite-to-postgres.sh"
    ).read_text(encoding="utf-8")

    stop_position = script.index("compose_sqlite stop web api")
    import_position = script.index("python migrate_sqlite_to_postgres.py")
    verify_position = script.index("--verify-only")
    authority_position = script.index("printf '%s\\n' postgresql")
    web_start_position = script.index("--force-recreate web")
    assert stop_position < import_position < verify_position
    assert verify_position < authority_position < web_start_position
    assert "restore_sqlite_service" in script
    assert '--env-file "$runtime_env" --env-file "$release_env"' in script
    assert "compose_sqlite run --rm --no-deps --entrypoint python api" in script
    assert "python3 -c" not in script
    assert 'DEPLOY_MODE=${DEPLOY_MODE:-production}' in script
    assert '-f "$compose_file" -f "$rollback_override" -f "$mode_override"' in script
    assert 'demo)\n    mode_override="$demo_override"' in script


def test_demo_deploy_bootstraps_postgresql_and_never_reverts_authority():
    script = (
        PROJECT_ROOT / "deploy/scripts/remote-build-deploy.sh"
    ).read_text(encoding="utf-8")

    build_position = script.index("docker build")
    release_env_position = script.index('mv "$release_env_next" "$release_env"')
    cutover_position = script.index('DEPLOY_MODE="$DEPLOY_MODE" "$cutover_script"')
    deploy_position = script.index("if ! compose up -d")

    assert build_position < release_env_position < cutover_position < deploy_position
    assert '""|sqlite)' in script
    assert 'if [ "$DEPLOY_MODE" != "demo" ]' in script
    assert "secrets.token_hex(32)" in script
    assert 'chmod 600 "$runtime_env_next"' in script
    assert "restore_previous_sqlite_release" in script
    assert "refusing to roll back to SQLite" in script


def test_api_image_installs_postgresql_driver():
    requirements = (PROJECT_ROOT / "apps/api/requirements.txt").read_text(
        encoding="utf-8"
    )

    assert "psycopg[binary]" in requirements
