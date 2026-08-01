from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..demo_personas import LOCAL_DEMO_PASSWORD, LOCAL_DEMO_PERSONAS
from ..infrastructure.tables import LocalCredential, User, now


PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=5)
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("not-a-real-local-password")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _username(value: str) -> str:
    return value.strip().lower()


class LocalCredentialService:
    """Development-only local authentication behind the normal Slow session."""

    def __init__(self, db: Session):
        self.db = db

    def ensure_seed_accounts(self) -> None:
        for persona in LOCAL_DEMO_PERSONAS:
            user = self.db.get(User, persona.user_id)
            if not user:
                user = User(
                    id=persona.user_id,
                    name=persona.display_name,
                    status="active",
                )
                self.db.add(user)
                self.db.flush()
            elif user.name != persona.display_name:
                user.name = persona.display_name
                user.updated_at = now()

            credential = self.db.scalar(
                select(LocalCredential).where(
                    LocalCredential.user_id == persona.user_id,
                )
            )
            if credential:
                continue
            self.db.add(
                LocalCredential(
                    id=f"local_credential_{uuid4().hex}",
                    user_id=persona.user_id,
                    username=_username(persona.username),
                    password_hash=PASSWORD_HASHER.hash(LOCAL_DEMO_PASSWORD),
                )
            )
        self.db.commit()

    def authenticate(self, *, username: str, password: str) -> User:
        credential = self.db.scalar(
            select(LocalCredential).where(
                LocalCredential.username == _username(username),
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
            code="LOCAL_LOGIN_INVALID",
            status=401,
        )
