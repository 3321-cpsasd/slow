import re

from sqlalchemy import select

import manage_users
from app.auth.password import PASSWORD_HASHER
from app.infrastructure.database import build_database
from app.infrastructure.tables import Base, LocalCredential, User


def test_generated_password_is_strong_and_copy_friendly():
    password = manage_users.generated_password()

    assert len(password) == manage_users.PASSWORD_LENGTH
    assert any(character in manage_users.PASSWORD_LOWERCASE for character in password)
    assert any(character in manage_users.PASSWORD_UPPERCASE for character in password)
    assert any(character in manage_users.PASSWORD_DIGITS for character in password)
    assert any(character in manage_users.PASSWORD_SYMBOLS for character in password)
    assert "'" not in password
    assert '"' not in password
    assert "\\" not in password


def test_create_demo_cli_persists_account_and_prints_password_once(
    tmp_path,
    monkeypatch,
    capsys,
):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'manage-users.db'}"
    engine, sessions = build_database(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setattr(manage_users.settings, "database_url", database_url)
    monkeypatch.setattr(manage_users.settings, "app_mode", "production")
    monkeypatch.setattr(manage_users.settings, "password_escrow_enabled", False)
    monkeypatch.setattr(
        manage_users.sys,
        "argv",
        ["manage_users.py", "create-demo", "--name", "演示用户"],
    )

    assert manage_users.main() == 0
    output = capsys.readouterr().out
    username = re.search(r"账号：(slow-demo\d{5})", output).group(1)
    password = re.search(r"初始密码：([^\n]+)", output).group(1)
    assert output.count(password) == 1

    engine, sessions = build_database(database_url)
    with sessions() as db:
        user = db.scalar(select(User))
        credential = db.scalar(select(LocalCredential))
        assert user.name == "演示用户"
        assert credential.username == username
        assert credential.password_hash != password
        assert PASSWORD_HASHER.verify(credential.password_hash, password)
    engine.dispose()


def test_create_demo_account_retries_username_collision(monkeypatch):
    usernames = iter(["slow-demo12345", "slow-demo67890"])
    monkeypatch.setattr(manage_users, "generated_demo_username", lambda: next(usernames))

    class FakeService:
        def __init__(self):
            self.calls = []

        def create_account(self, *, username, display_name, password):
            self.calls.append((username, display_name, password))
            if len(self.calls) == 1:
                raise ValueError("账号已存在")
            return User(id="user_test", name=display_name, status="active")

    service = FakeService()
    user, username, password = manage_users.create_demo_account(service)

    assert username == "slow-demo67890"
    assert user.name == username
    assert len(password) == manage_users.PASSWORD_LENGTH
    assert [call[0] for call in service.calls] == [
        "slow-demo12345",
        "slow-demo67890",
    ]
