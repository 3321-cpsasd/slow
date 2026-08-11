from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import AuthSession, LocalCredential, User, now


PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=5)
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("not-a-real-password")


def normalize_username(value: str) -> str:
    return value.strip().lower()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PasswordCredentialService:
    """Authoritative username/password credential service for invited users."""

    def __init__(self, db: Session):
        self.db = db

    def create_account(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        registration_source: str = "unspecified",
        registration_quota_date: str | None = None,
        commit: bool = True,
    ) -> User:
        normalized = normalize_username(username)
        if not 3 <= len(normalized) <= 80:
            raise ValueError("账号长度必须为 3 到 80 个字符")
        if not normalized[0].isalnum() or any(
            not (character.isalnum() or character in "._-")
            for character in normalized
        ):
            raise ValueError("账号只能包含文字、数字、点、下划线和连字符")
        if not display_name.strip():
            raise ValueError("显示名称不能为空")
        if not 12 <= len(password) <= 200:
            raise ValueError("密码长度必须为 12 到 200 个字符")

        user = User(
            id=f"user_{uuid4().hex}",
            name=display_name.strip()[:120],
            status="active",
        )
        self.db.add(user)
        self.db.flush()
        credential = LocalCredential(
            id=f"password_credential_{uuid4().hex}",
            user_id=user.id,
            username=normalized,
            password_hash=PASSWORD_HASHER.hash(password),
            registration_source=registration_source,
            registration_quota_date=registration_quota_date,
            status="active",
        )
        self.db.add(credential)
        try:
            self.db.flush()
            if commit:
                self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            raise ValueError("账号已存在") from error
        return user

    def set_account_enabled(self, *, username: str, enabled: bool) -> User:
        credential = self._credential(username)
        user = self.db.get(User, credential.user_id)
        if not user:
            raise ValueError("账号引用的用户不存在")
        status = "active" if enabled else "disabled"
        credential.status = status
        credential.updated_at = now()
        if enabled:
            credential.failed_attempts = 0
            credential.locked_until = None
        user.status = status
        user.updated_at = now()
        if not enabled:
            self._revoke_user_sessions(user.id)
        self.db.commit()
        return user

    def reset_password(
        self,
        *,
        username: str,
        password: str,
        commit: bool = True,
    ) -> User:
        if not 12 <= len(password) <= 200:
            raise ValueError("密码长度必须为 12 到 200 个字符")
        credential = self._credential(username)
        user = self.db.get(User, credential.user_id)
        if not user:
            raise ValueError("账号引用的用户不存在")
        current = now()
        credential.password_hash = PASSWORD_HASHER.hash(password)
        credential.failed_attempts = 0
        credential.locked_until = None
        credential.password_changed_at = current
        credential.updated_at = current
        self._revoke_user_sessions(user.id)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return user

    def authenticate(self, *, username: str, password: str) -> User:
        credential = self.db.scalar(
            select(LocalCredential).where(
                LocalCredential.username == normalize_username(username),
            )
        )
        current = now()
        if not credential:
            self._consume_dummy_password(password)
            raise self._invalid_credentials()
        if credential.status != "active":
            self._consume_dummy_password(password)
            raise self._invalid_credentials()
        if credential.locked_until and _aware(credential.locked_until) > current:
            self._consume_dummy_password(password)
            raise self._invalid_credentials()

        if credential.locked_until:
            credential.locked_until = None
            credential.failed_attempts = 0

        try:
            verified = PASSWORD_HASHER.verify(credential.password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            verified = False
        if not verified:
            credential.failed_attempts += 1
            if credential.failed_attempts >= MAX_FAILED_ATTEMPTS:
                credential.locked_until = current + LOCK_DURATION
                credential.failed_attempts = 0
            credential.updated_at = current
            self.db.commit()
            raise self._invalid_credentials()

        user = self.db.get(User, credential.user_id)
        if not user or user.status != "active":
            raise AppError(
                "账户当前不可用",
                code="ACCOUNT_DISABLED",
                status=403,
            )
        if PASSWORD_HASHER.check_needs_rehash(credential.password_hash):
            credential.password_hash = PASSWORD_HASHER.hash(password)
            credential.password_changed_at = current
        credential.failed_attempts = 0
        credential.locked_until = None
        credential.last_login_at = current
        credential.updated_at = current
        self.db.commit()
        return user

    def _credential(self, username: str) -> LocalCredential:
        credential = self.db.scalar(
            select(LocalCredential).where(
                LocalCredential.username == normalize_username(username),
            )
        )
        if not credential:
            raise ValueError("账号不存在")
        return credential

    def _revoke_user_sessions(self, user_id: str) -> None:
        current = now()
        self.db.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.status == "active",
            )
            .values(status="revoked", revoked_at=current)
        )

    @staticmethod
    def _consume_dummy_password(password: str) -> None:
        try:
            PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass

    @staticmethod
    def _invalid_credentials() -> AppError:
        return AppError(
            "账号或密码错误",
            code="PASSWORD_LOGIN_INVALID",
            status=401,
        )
