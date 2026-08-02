import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import (
    AuthSession,
    OidcLoginState,
    User,
    UserIdentity,
    now,
)
from .context import Principal, UserScope


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SessionService:
    def __init__(
        self,
        db: Session,
        *,
        ttl_seconds: int,
        idle_timeout_seconds: int | None = None,
    ):
        self.db = db
        self.ttl = timedelta(seconds=ttl_seconds)
        self.idle_timeout = (
            timedelta(seconds=idle_timeout_seconds)
            if idle_timeout_seconds is not None
            else None
        )

    def issue(self, user: User) -> tuple[AuthSession, str, str]:
        if user.status != "active":
            raise AppError(
                "账户当前不可登录",
                code="ACCOUNT_DISABLED",
                status=403,
            )
        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        session = AuthSession(
            id=_uid("session"),
            user_id=user.id,
            token_hash=_hash(raw_token),
            csrf_token_hash=_hash(csrf_token),
            status="active",
            expires_at=now() + self.ttl,
        )
        self.db.add(session)
        self.db.commit()
        return session, raw_token, csrf_token

    def authenticate(self, raw_token: str | None) -> tuple[UserScope, AuthSession]:
        if not raw_token:
            raise AppError(
                "请先登录",
                code="AUTHENTICATION_REQUIRED",
                status=401,
            )
        session = self.db.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == _hash(raw_token),
                AuthSession.status == "active",
            )
        )
        current = now()
        if not session or _aware(session.expires_at) <= current:
            if session and session.status == "active":
                session.status = "expired"
                self.db.commit()
            raise AppError(
                "登录状态已失效",
                code="SESSION_EXPIRED",
                status=401,
            )
        if (
            self.idle_timeout is not None
            and current - _aware(session.last_seen_at) >= self.idle_timeout
        ):
            session.status = "expired"
            self.db.commit()
            raise AppError(
                "登录状态因长时间未使用而失效",
                code="SESSION_IDLE_EXPIRED",
                status=401,
            )
        user = self.db.get(User, session.user_id)
        if not user or user.status != "active":
            session.status = "revoked"
            session.revoked_at = current
            self.db.commit()
            raise AppError(
                "账户当前不可用",
                code="ACCOUNT_DISABLED",
                status=403,
            )
        if current - _aware(session.last_seen_at) >= timedelta(minutes=5):
            session.last_seen_at = current
            self.db.commit()
        return (
            UserScope(
                Principal(
                    actor_kind="user",
                    actor_id=user.id,
                    subject_user_id=user.id,
                    session_id=session.id,
                )
            ),
            session,
        )

    def require_csrf(self, session: AuthSession, csrf_token: str | None) -> None:
        if not csrf_token or not hmac.compare_digest(
            session.csrf_token_hash,
            _hash(csrf_token),
        ):
            raise AppError(
                "请求缺少有效的 CSRF 凭证",
                code="CSRF_INVALID",
                status=403,
            )

    def revoke(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        session = self.db.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == _hash(raw_token),
            )
        )
        if not session:
            return
        session.status = "revoked"
        session.revoked_at = now()
        self.db.commit()


class IdentityService:
    def __init__(self, db: Session):
        self.db = db

    def resolve_or_create(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str,
        email: str,
        email_verified: bool,
    ) -> User:
        identity = self.db.scalar(
            select(UserIdentity).where(
                UserIdentity.issuer == issuer,
                UserIdentity.subject == subject,
            )
        )
        if identity:
            user = self.db.get(User, identity.user_id)
            if not user:
                raise AppError(
                    "身份映射引用的用户不存在",
                    code="IDENTITY_USER_MISSING",
                    status=500,
                )
            identity.email_snapshot = email
            identity.email_verified = email_verified
            identity.updated_at = now()
            if display_name and user.name != display_name:
                user.name = display_name[:120]
                user.updated_at = now()
            self.db.commit()
            return user

        user = User(
            id=_uid("user"),
            name=(display_name or email or "学习者")[:120],
            status="active",
        )
        identity = UserIdentity(
            id=_uid("identity"),
            user_id=user.id,
            issuer=issuer,
            subject=subject,
            email_snapshot=email,
            email_verified=email_verified,
        )
        self.db.add_all([user, identity])
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(
                select(UserIdentity).where(
                    UserIdentity.issuer == issuer,
                    UserIdentity.subject == subject,
                )
            )
            if not existing:
                raise
            user = self.db.get(User, existing.user_id)
        return user


class OidcStateService:
    def __init__(self, db: Session):
        self.db = db

    def create(self, *, return_to: str) -> tuple[str, OidcLoginState]:
        self.db.execute(
            delete(OidcLoginState).where(
                OidcLoginState.expires_at < now(),
            )
        )
        raw_state = secrets.token_urlsafe(32)
        row = OidcLoginState(
            state_hash=_hash(raw_state),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            return_to=return_to,
            expires_at=now() + timedelta(minutes=10),
        )
        self.db.add(row)
        self.db.commit()
        return raw_state, row

    def consume(self, raw_state: str) -> OidcLoginState:
        row = self.db.get(OidcLoginState, _hash(raw_state))
        if not row or _aware(row.expires_at) <= now():
            raise AppError(
                "登录请求已过期或无效",
                code="OIDC_STATE_INVALID",
                status=400,
            )
        self.db.delete(row)
        self.db.commit()
        return row


def token_hash(value: str) -> str:
    return _hash(value)
