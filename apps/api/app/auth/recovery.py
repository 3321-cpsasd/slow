import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import (
    AccountRecoveryCode,
    LocalCredential,
    now,
)
from .password import PasswordCredentialService


MAX_RECOVERY_ATTEMPTS = 5
RECOVERY_LOCK_DURATION = timedelta(minutes=15)
_DUMMY_RECOVERY_HASH = hashlib.sha256(b"not-a-real-recovery-code").hexdigest()


def normalize_recovery_code(value: str) -> str:
    return "".join(value.strip().upper().split()).replace("-", "")


def recovery_code_hash(value: str) -> str:
    return hashlib.sha256(normalize_recovery_code(value).encode()).hexdigest()


def generate_recovery_code() -> str:
    secret = secrets.token_hex(16).upper()
    return "SLOW-" + "-".join(
        secret[index:index + 4] for index in range(0, len(secret), 4)
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class AccountRecoveryService:
    def __init__(self, db: Session):
        self.db = db

    def issue(self, *, user_id: str, commit: bool = True) -> str:
        current = now()
        active = self.db.scalars(
            select(AccountRecoveryCode).where(
                AccountRecoveryCode.user_id == user_id,
                AccountRecoveryCode.status == "active",
            )
        ).all()
        for item in active:
            item.status = "revoked"
            item.revoked_at = current
        version = int(self.db.scalar(
            select(func.max(AccountRecoveryCode.version)).where(
                AccountRecoveryCode.user_id == user_id,
            )
        ) or 0) + 1
        raw_code = generate_recovery_code()
        self.db.add(AccountRecoveryCode(
            id=f"account_recovery_{uuid4().hex}",
            user_id=user_id,
            version=version,
            code_hash=recovery_code_hash(raw_code),
            status="active",
        ))
        self.db.flush()
        if commit:
            self.db.commit()
        return raw_code

    def reset_password(
        self,
        *,
        username: str,
        recovery_code: str,
        new_password: str,
    ) -> str:
        credential = self.db.scalar(
            select(LocalCredential).where(
                LocalCredential.username == username.strip().lower(),
                LocalCredential.status == "active",
            )
        )
        recovery = None
        if credential:
            recovery = self.db.scalar(
                select(AccountRecoveryCode)
                .where(
                    AccountRecoveryCode.user_id == credential.user_id,
                    AccountRecoveryCode.status == "active",
                )
                .order_by(AccountRecoveryCode.version.desc())
            )
        current = now()
        supplied_hash = recovery_code_hash(recovery_code)
        expected_hash = recovery.code_hash if recovery else _DUMMY_RECOVERY_HASH
        locked = bool(
            recovery
            and recovery.locked_until
            and _aware(recovery.locked_until) > current
        )
        valid = bool(
            recovery
            and not locked
            and hmac.compare_digest(expected_hash, supplied_hash)
        )
        if not valid:
            if recovery and not locked:
                recovery.failed_attempts += 1
                if recovery.failed_attempts >= MAX_RECOVERY_ATTEMPTS:
                    recovery.failed_attempts = 0
                    recovery.locked_until = current + RECOVERY_LOCK_DURATION
                self.db.commit()
            raise AppError(
                "账号或恢复码无效",
                code="ACCOUNT_RECOVERY_INVALID",
                status=401,
            )

        recovery.status = "used"
        recovery.used_at = current
        recovery.failed_attempts = 0
        recovery.locked_until = None
        PasswordCredentialService(self.db).reset_password(
            username=credential.username,
            password=new_password,
            commit=False,
        )
        replacement = self.issue(user_id=credential.user_id, commit=False)
        self.db.commit()
        return replacement
