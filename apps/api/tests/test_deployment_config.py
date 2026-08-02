from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_login_rate_limit_uses_path_without_query_string():
    nginx_config = (PROJECT_ROOT / "deploy/nginx/default.conf").read_text(
        encoding="utf-8"
    )

    assert "map $uri $slow_login_limit_key" in nginx_config
    assert "map $request_uri $slow_login_limit_key" not in nginx_config
    assert "~^/api/auth/(password|local)/login$ $binary_remote_addr;" in nginx_config
