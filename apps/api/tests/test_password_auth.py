from datetime import timedelta
import stat

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.local_adapter import LocalDemoAdapter
from app.auth.password import PasswordCredentialService
from app.auth.password_escrow import PasswordEscrowStore
from app.auth.service import SessionService
from app.infrastructure.tables import (
    AccountExitRequest,
    AuthSession,
    LocalCredential,
    PrivacyConsent,
    Shelf,
    User,
    now,
)
from app.main import create_app
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


USERNAME = "beta-user"
DISPLAY_NAME = "内测用户"
PASSWORD = "Correct-Beta-Password-2026"


def password_app(tmp_path):
    return create_app(
        f"sqlite+pysqlite:///{tmp_path / 'password-auth.db'}",
        ai=LocalDemoAdapter(),
        source_verifier=AcceptingSourceVerifier(),
        attachment_storage=LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="password",
        app_mode="production",
        runtime_settings_path=False,
    )


def create_account(client: TestClient):
    with client.app.state.sessions() as db:
        return PasswordCredentialService(db).create_account(
            username=USERNAME,
            display_name=DISPLAY_NAME,
            password=PASSWORD,
        )


def login(client: TestClient, password: str = PASSWORD):
    return client.post(
        "/api/auth/password/login",
        json={"username": USERNAME, "password": password},
    )


def complete_profile(client: TestClient, csrf_token: str):
    consent = client.post(
        "/api/privacy/consent",
        headers={"X-CSRF-Token": csrf_token},
        json={"privacyAccepted": True, "trialAccepted": True},
    )
    assert consent.status_code == 200
    return client.post(
        "/api/onboarding/profile/complete",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "profession": "产品设计师",
            "stage": "foundation",
            "purpose": "系统学习信息可视化并完成作品集项目",
            "domains": ["信息可视化", "交互设计"],
            "experience": "有产品设计经验，缺少数据表达基础",
        },
    )


def test_production_password_login_requires_precreated_account(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        config = client.get("/api/auth/config")
        assert config.status_code == 200
        assert config.json()["mode"] == "password"
        assert config.json()["providerName"] == ""
        assert config.json()["privacyNotice"]["noticeVersion"] == "2026-08-12-r3"

        missing = login(client)
        assert missing.status_code == 401
        assert missing.json()["code"] == "PASSWORD_LOGIN_INVALID"

        user = create_account(client)
        logged_in = login(client)
        assert logged_in.status_code == 200
        assert logged_in.json()["mode"] == "password"
        assert logged_in.json()["user"] == {
            "id": user.id,
            "name": DISPLAY_NAME,
        }
        assert logged_in.cookies.get("slow_session")
        assert logged_in.cookies.get("slow_csrf")
        assert logged_in.json()["privacy"]["required"] is True
        assert logged_in.json()["onboarding"]["required"] is True

        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 428
        assert bootstrap.json()["code"] == "PRIVACY_CONSENT_REQUIRED"

        completed = complete_profile(client, logged_in.json()["csrfToken"])
        assert completed.status_code == 200
        assert completed.json()["required"] is False
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["user"] == {
            "id": user.id,
            "name": DISPLAY_NAME,
        }
        assert bootstrap.json()["shelves"] == []
        with client.app.state.sessions() as db:
            assert len(db.scalars(select(User)).all()) == 1
            assert db.scalars(select(Shelf)).all() == []
            credential = db.scalar(select(LocalCredential))
            assert credential.password_hash.startswith("$argon2id$")
            assert credential.password_hash != PASSWORD


def test_privacy_consent_is_versioned_and_required_before_learning(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        user = create_account(client)
        logged_in = login(client)
        csrf = logged_in.json()["csrfToken"]

        incomplete = client.post(
            "/api/privacy/consent",
            headers={"X-CSRF-Token": csrf},
            json={"privacyAccepted": True, "trialAccepted": False},
        )
        assert incomplete.status_code == 400
        assert incomplete.json()["code"] == "PRIVACY_CONSENT_INCOMPLETE"

        accepted = client.post(
            "/api/privacy/consent",
            headers={"X-CSRF-Token": csrf},
            json={"privacyAccepted": True, "trialAccepted": True},
        )
        assert accepted.status_code == 200
        assert accepted.json()["required"] is False
        assert accepted.json()["status"] == "accepted"
        assert accepted.json()["acceptedAt"]

        replay = client.post(
            "/api/privacy/consent",
            headers={"X-CSRF-Token": csrf},
            json={"privacyAccepted": True, "trialAccepted": True},
        )
        assert replay.status_code == 200
        with client.app.state.sessions() as db:
            consents = db.scalars(
                select(PrivacyConsent).where(PrivacyConsent.user_id == user.id)
            ).all()
            assert len(consents) == 1


def test_account_exit_revokes_sessions_and_creates_deletion_request(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        user = create_account(client)
        logged_in = login(client)
        csrf = logged_in.json()["csrfToken"]
        assert client.post(
            "/api/privacy/consent",
            headers={"X-CSRF-Token": csrf},
            json={"privacyAccepted": True, "trialAccepted": True},
        ).status_code == 200

        invalid = client.post(
            "/api/account/exit",
            headers={"X-CSRF-Token": csrf},
            json={"confirmation": "删除", "reason": ""},
        )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "ACCOUNT_EXIT_CONFIRMATION_INVALID"

        exited = client.post(
            "/api/account/exit",
            headers={"X-CSRF-Token": csrf},
            json={"confirmation": "退出并删除", "reason": "不再参加试点"},
        )
        assert exited.status_code == 202
        assert exited.json()["status"] == "requested"
        assert exited.json()["deletionDueAt"]

        with client.app.state.sessions() as db:
            assert db.get(User, user.id).status == "exit_requested"
            credential = db.scalar(
                select(LocalCredential).where(LocalCredential.user_id == user.id)
            )
            assert credential.status == "disabled"
            assert not db.scalar(
                select(AuthSession).where(
                    AuthSession.user_id == user.id,
                    AuthSession.status == "active",
                )
            )
            request = db.scalar(
                select(AccountExitRequest).where(AccountExitRequest.user_id == user.id)
            )
            assert request.status == "requested"
            assert request.reason == "不再参加试点"

        client.cookies.clear()
        assert login(client).status_code == 401


def test_password_session_has_absolute_and_idle_expiry(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        create_account(client)
        assert login(client).status_code == 200
        with client.app.state.sessions() as db:
            session = db.scalar(
                select(AuthSession).where(AuthSession.status == "active")
            )
            session.last_seen_at = now() - timedelta(days=2)
            db.commit()

        idle_expired = client.get("/api/bootstrap")
        assert idle_expired.status_code == 401
        assert idle_expired.json()["code"] == "SESSION_IDLE_EXPIRED"

        client.cookies.clear()
        assert login(client).status_code == 200
        with client.app.state.sessions() as db:
            session = db.scalar(
                select(AuthSession).where(AuthSession.status == "active")
            )
            session.expires_at = now() - timedelta(seconds=1)
            db.commit()

        absolute_expired = client.get("/api/bootstrap")
        assert absolute_expired.status_code == 401
        assert absolute_expired.json()["code"] == "SESSION_EXPIRED"


def test_disable_and_password_reset_revoke_all_user_sessions(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        user = create_account(client)
        with client.app.state.sessions() as db:
            sessions = SessionService(db, ttl_seconds=604800)
            sessions.issue(db.get(User, user.id))
            sessions.issue(db.get(User, user.id))
            PasswordCredentialService(db).set_account_enabled(
                username=USERNAME,
                enabled=False,
            )
            statuses = db.scalars(
                select(AuthSession.status).where(AuthSession.user_id == user.id)
            ).all()
            assert statuses == ["revoked", "revoked"]

        disabled = login(client)
        assert disabled.status_code == 401
        assert disabled.json()["code"] == "PASSWORD_LOGIN_INVALID"

        with client.app.state.sessions() as db:
            service = PasswordCredentialService(db)
            service.set_account_enabled(username=USERNAME, enabled=True)
        logged_in = login(client)
        assert logged_in.status_code == 200
        completed = complete_profile(
            client,
            logged_in.json()["csrfToken"],
        )
        assert completed.status_code == 200
        assert client.get("/api/bootstrap").json()["shelves"] == []
        rejected_metadata = client.post(
            "/api/shelves",
            headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
            json={"name": "旧版书架", "domain": "不再接受手填分类"},
        )
        assert rejected_metadata.status_code == 400
        rejected_blank_name = client.post(
            "/api/shelves",
            headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
            json={"name": "   "},
        )
        assert rejected_blank_name.status_code == 400
        created = client.post(
            "/api/shelves",
            headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
            json={"name": "我的学习书架"},
        )
        assert created.status_code == 201
        assert created.json()["domain"] == ""
        assert created.json()["specialty"] == ""
        assert created.json()["tags"] == []
        shelf_id = created.json()["id"]

        missing_rename_csrf = client.patch(
            f"/api/shelves/{shelf_id}",
            json={"name": "未授权改名"},
        )
        assert missing_rename_csrf.status_code == 403
        rejected_rename_metadata = client.patch(
            f"/api/shelves/{shelf_id}",
            headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
            json={"name": "新名称", "domain": "不允许同时修改"},
        )
        assert rejected_rename_metadata.status_code == 400
        rejected_blank_rename = client.patch(
            f"/api/shelves/{shelf_id}",
            headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
            json={"name": "   "},
        )
        assert rejected_blank_rename.status_code == 400
        renamed = client.patch(
            f"/api/shelves/{shelf_id}",
            headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
            json={"name": "  我的   新书架  "},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "我的 新书架"
        assert client.get("/api/bootstrap").json()["shelves"][0]["name"] == "我的 新书架"
        missing_delete_csrf = client.delete(f"/api/shelves/{shelf_id}")
        assert missing_delete_csrf.status_code == 403

        new_password = "A-New-Beta-Password-2026"
        with client.app.state.sessions() as db:
            PasswordCredentialService(db).reset_password(
                username=USERNAME,
                password=new_password,
            )
            assert not db.scalar(
                select(AuthSession).where(
                    AuthSession.user_id == user.id,
                    AuthSession.status == "active",
                )
            )

        revoked = client.get("/api/bootstrap")
        assert revoked.status_code == 401
        client.cookies.clear()
        assert login(client).status_code == 401
        assert login(client, new_password).status_code == 200
        assert client.get("/api/bootstrap").json()["shelves"][0]["id"] == shelf_id


def test_password_endpoint_is_not_available_in_local_mode(tmp_path):
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        ai=LocalDemoAdapter(),
        auth_mode="local",
        app_mode="development",
        runtime_settings_path=False,
    )
    with TestClient(app) as client:
        response = login(client)
    assert response.status_code == 404
    assert response.json()["code"] == "PASSWORD_AUTH_NOT_ENABLED"


def test_password_validation_error_does_not_echo_password(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/auth/password/login",
            json={"username": USERNAME, "password": "secret"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert "secret" not in response.text
    detail = response.json()["details"][0]
    assert detail["type"] == "too_short"
    assert detail["loc"] == ["body", "password"]
    assert "input" not in detail


def test_account_creation_rejects_invalid_username_and_duplicate(tmp_path):
    app = password_app(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        with client.app.state.sessions() as db:
            service = PasswordCredentialService(db)
            with pytest.raises(ValueError, match="账号只能包含"):
                service.create_account(
                    username="bad account",
                    display_name=DISPLAY_NAME,
                    password=PASSWORD,
                )

            service.create_account(
                username=USERNAME,
                display_name=DISPLAY_NAME,
                password=PASSWORD,
            )
            with pytest.raises(ValueError, match="账号已存在"):
                service.create_account(
                    username=USERNAME.upper(),
                    display_name="重复用户",
                    password=PASSWORD,
                )


def test_development_password_escrow_is_explicit_private_and_purgeable(tmp_path):
    path = tmp_path / "password-escrow.json"
    disabled = PasswordEscrowStore(
        path,
        enabled=False,
        app_mode="development",
    )
    with pytest.raises(RuntimeError, match="密码托管未启用"):
        disabled.reveal(username=USERNAME)

    escrow = PasswordEscrowStore(
        path,
        enabled=True,
        app_mode="development",
    )
    escrow.record(username=USERNAME.upper(), password=PASSWORD)
    assert escrow.reveal(username=USERNAME) == PASSWORD
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "development-password-escrow" in path.read_text(encoding="utf-8")

    assert escrow.purge() is True
    assert not path.exists()
    assert escrow.purge() is False


def test_production_rejects_password_escrow(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="生产环境禁止"):
        PasswordEscrowStore(
            tmp_path / "password-escrow.json",
            enabled=True,
            app_mode="production",
        )

    from app.core.config import settings

    monkeypatch.setattr(settings, "password_escrow_enabled", True)
    with pytest.raises(
        RuntimeError,
        match="Production mode cannot enable password escrow",
    ):
        password_app(tmp_path)
