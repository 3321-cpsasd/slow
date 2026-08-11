from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.ai.local_adapter import LocalDemoAdapter
from app.infrastructure.tables import (
    AccountRecoveryCode,
    AlphaRegistrationQuota,
    AuthSession,
    LocalCredential,
    User,
)
from app.auth.registration import _quota_insert_statement, claim_alpha_registration
from app.core.errors import AppError
from app.main import create_app
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


ALPHA_CODE = "alpha-preview-2026"
USERNAME = "new-learner"
PASSWORD = "Strong-Alpha-Password-2026"


def alpha_app(tmp_path, *, registration_mode="alpha", daily_limit=100):
    return create_app(
        f"sqlite+pysqlite:///{tmp_path / 'alpha-auth.db'}",
        ai=LocalDemoAdapter(),
        source_verifier=AcceptingSourceVerifier(),
        attachment_storage=LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="password",
        app_mode="production",
        registration_mode=registration_mode,
        alpha_registration_code=(
            ALPHA_CODE if registration_mode == "alpha" else ""
        ),
        alpha_registration_daily_limit=daily_limit,
        runtime_settings_path=False,
    )


def register(client, **overrides):
    body = {
        "username": USERNAME,
        "password": PASSWORD,
        "passwordConfirm": PASSWORD,
        "alphaCode": ALPHA_CODE,
        **overrides,
    }
    return client.post("/api/auth/password/register", json=body)


def login(client, password=PASSWORD):
    return client.post(
        "/api/auth/password/login",
        json={"username": USERNAME, "password": password},
    )


def test_alpha_registration_creates_recoverable_account_and_session(tmp_path):
    with TestClient(
        alpha_app(tmp_path), base_url="https://testserver"
    ) as client:
        config = client.get("/api/auth/config")
        assert config.status_code == 200
        assert config.json()["registrationMode"] == "alpha"
        assert config.json()["registrationCodeRequired"] is True

        rejected = register(client, alphaCode="wrong-code")
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "ALPHA_ACCESS_CODE_INVALID"

        created = register(client)
        assert created.status_code == 201
        assert created.json()["authenticated"] is True
        assert created.json()["user"]["name"] == USERNAME
        assert created.json()["recoveryCode"].startswith("SLOW-")
        assert created.cookies.get("slow_session")
        assert created.json()["privacy"]["required"] is True

        raw_recovery_code = created.json()["recoveryCode"]
        with client.app.state.sessions() as db:
            assert len(db.scalars(select(User)).all()) == 1
            credential = db.scalar(select(LocalCredential))
            recovery = db.scalar(select(AccountRecoveryCode))
            assert credential.password_hash.startswith("$argon2id$")
            assert credential.registration_source == "alpha_self_service"
            assert credential.registration_quota_date
            quota = db.scalar(select(AlphaRegistrationQuota))
            assert quota.used_count == 1
            assert recovery.status == "active"
            assert recovery.version == 1
            assert raw_recovery_code not in recovery.code_hash
            assert len(recovery.code_hash) == 64


def test_registration_is_closed_by_default_and_daily_limit_fails_closed(tmp_path):
    with TestClient(
        alpha_app(tmp_path, registration_mode="closed"),
        base_url="https://testserver",
    ) as client:
        assert client.get("/api/auth/config").json()["registrationMode"] == "closed"
        assert register(client).status_code == 404

    limited_path = tmp_path / "limited"
    limited_path.mkdir()
    with TestClient(
        alpha_app(limited_path, daily_limit=1),
        base_url="https://testserver",
    ) as client:
        assert register(client).status_code == 201
        client.cookies.clear()
        limited = register(client, username="second-learner")
        assert limited.status_code == 429
        assert limited.json()["code"] == "REGISTRATION_DAILY_LIMIT_REACHED"


def test_recovery_reset_rotates_code_revokes_sessions_and_hides_account_lookup(tmp_path):
    with TestClient(
        alpha_app(tmp_path), base_url="https://testserver"
    ) as client:
        created = register(client)
        old_recovery_code = created.json()["recoveryCode"]
        assert login(client).status_code == 200

        unknown = client.post(
            "/api/auth/password/recover",
            json={
                "username": "missing-user",
                "recoveryCode": old_recovery_code,
                "newPassword": "Another-Strong-Password-2026",
                "newPasswordConfirm": "Another-Strong-Password-2026",
            },
        )
        invalid = client.post(
            "/api/auth/password/recover",
            json={
                "username": USERNAME,
                "recoveryCode": "SLOW-0000-0000-0000-0000-0000-0000-0000-0000",
                "newPassword": "Another-Strong-Password-2026",
                "newPasswordConfirm": "Another-Strong-Password-2026",
            },
        )
        assert unknown.status_code == invalid.status_code == 401
        assert unknown.json()["message"] == invalid.json()["message"]

        new_password = "Another-Strong-Password-2026"
        reset = client.post(
            "/api/auth/password/recover",
            json={
                "username": USERNAME,
                "recoveryCode": old_recovery_code.lower().replace("-", " "),
                "newPassword": new_password,
                "newPasswordConfirm": new_password,
            },
        )
        assert reset.status_code == 200
        replacement_code = reset.json()["recoveryCode"]
        assert replacement_code.startswith("SLOW-")
        assert replacement_code != old_recovery_code

        assert client.get("/api/bootstrap").status_code == 401
        client.cookies.clear()
        assert login(client).status_code == 401
        assert login(client, new_password).status_code == 200

        reused = client.post(
            "/api/auth/password/recover",
            json={
                "username": USERNAME,
                "recoveryCode": old_recovery_code,
                "newPassword": "Third-Strong-Password-2026",
                "newPasswordConfirm": "Third-Strong-Password-2026",
            },
        )
        assert reused.status_code == 401
        with client.app.state.sessions() as db:
            codes = db.scalars(
                select(AccountRecoveryCode)
                .where(AccountRecoveryCode.user_id == db.scalar(select(User.id)))
                .order_by(AccountRecoveryCode.version)
            ).all()
            assert [(item.version, item.status) for item in codes] == [
                (1, "used"),
                (2, "active"),
            ]
            assert len(db.scalars(
                select(AuthSession).where(AuthSession.status == "active")
            ).all()) == 1


def test_authenticated_user_can_rotate_a_lost_recovery_code(tmp_path):
    with TestClient(
        alpha_app(tmp_path), base_url="https://testserver"
    ) as client:
        created = register(client)
        old_code = created.json()["recoveryCode"]
        rotated = client.post(
            "/api/auth/password/recovery-code/rotate",
            headers={"X-CSRF-Token": created.json()["csrfToken"]},
            json={"currentPassword": PASSWORD},
        )
        assert rotated.status_code == 200
        assert rotated.json()["recoveryCode"].startswith("SLOW-")
        assert rotated.json()["recoveryCode"] != old_code
        with client.app.state.sessions() as db:
            codes = db.scalars(
                select(AccountRecoveryCode).order_by(AccountRecoveryCode.version)
            ).all()
            assert [(item.version, item.status) for item in codes] == [
                (1, "revoked"),
                (2, "active"),
            ]


def test_recovery_code_rotation_requires_the_current_password(tmp_path):
    with TestClient(
        alpha_app(tmp_path), base_url="https://testserver"
    ) as client:
        created = register(client)
        rejected = client.post(
            "/api/auth/password/recovery-code/rotate",
            headers={"X-CSRF-Token": created.json()["csrfToken"]},
            json={"currentPassword": "Wrong-Password-Value"},
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "ACCOUNT_REAUTH_INVALID"
        with client.app.state.sessions() as db:
            codes = db.scalars(select(AccountRecoveryCode)).all()
            assert [(item.version, item.status) for item in codes] == [
                (1, "active")
            ]


def test_recovery_code_reauthentication_uses_password_lockout(tmp_path):
    with TestClient(
        alpha_app(tmp_path), base_url="https://testserver"
    ) as client:
        created = register(client)
        headers = {"X-CSRF-Token": created.json()["csrfToken"]}

        for _ in range(5):
            rejected = client.post(
                "/api/auth/password/recovery-code/rotate",
                headers=headers,
                json={"currentPassword": "Wrong-Password-Value"},
            )
            assert rejected.status_code == 403
            assert rejected.json()["code"] == "ACCOUNT_REAUTH_INVALID"

        locked = client.post(
            "/api/auth/password/recovery-code/rotate",
            headers=headers,
            json={"currentPassword": PASSWORD},
        )
        assert locked.status_code == 403
        assert locked.json()["code"] == "ACCOUNT_REAUTH_INVALID"

        with client.app.state.sessions() as db:
            credential = db.scalar(select(LocalCredential))
            assert credential is not None
            assert credential.failed_attempts == 0
            assert credential.locked_until is not None
            assert db.scalar(select(AuthSession)).status == "active"
            assert db.scalar(select(AccountRecoveryCode)).status == "active"


def test_alpha_quota_insert_compiles_for_supported_production_dialects():
    sqlite_sql = str(_quota_insert_statement(
        dialect_name="sqlite",
        quota_date="2026-08-11",
        daily_limit=10,
    ).compile(dialect=sqlite.dialect()))
    postgres_sql = str(_quota_insert_statement(
        dialect_name="postgresql",
        quota_date="2026-08-11",
        daily_limit=10,
    ).compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sqlite_sql
    assert "ON CONFLICT" in postgres_sql


def test_alpha_quota_reservation_is_atomic_under_concurrency(tmp_path):
    app = alpha_app(tmp_path, daily_limit=1)
    with TestClient(app, base_url="https://testserver"):
        quota_date = "2026-08-11"
        barrier = Barrier(2)

        def reserve() -> bool:
            with app.state.sessions() as db:
                barrier.wait()
                try:
                    claim_alpha_registration(
                        db,
                        quota_date=quota_date,
                        daily_limit=1,
                    )
                    db.commit()
                    return True
                except AppError:
                    db.rollback()
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        assert sorted(results) == [False, True]
        with app.state.sessions() as db:
            assert db.get(AlphaRegistrationQuota, quota_date).used_count == 1


def test_registration_and_recovery_validation_never_echo_secrets(tmp_path):
    with TestClient(
        alpha_app(tmp_path), base_url="https://testserver"
    ) as client:
        secret = "secret-that-must-not-echo"
        mismatch = register(
            client,
            password=secret,
            passwordConfirm="different-secret-value",
        )
        assert mismatch.status_code == 400
        assert secret not in mismatch.text
        assert "different-secret-value" not in mismatch.text

        recovery_secret = "SLOW-1234-5678"
        invalid = client.post(
            "/api/auth/password/recover",
            json={
                "username": USERNAME,
                "recoveryCode": recovery_secret,
                "newPassword": secret,
                "newPasswordConfirm": "different-secret-value",
            },
        )
        assert invalid.status_code == 400
        assert recovery_secret not in invalid.text
        assert secret not in invalid.text
