from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..demo_personas import LOCAL_DEMO_PASSWORD, LOCAL_DEMO_PERSONAS
from ..infrastructure.tables import LocalCredential, User, now
from .password import PASSWORD_HASHER, PasswordCredentialService, normalize_username


class LocalCredentialService(PasswordCredentialService):
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
                    username=normalize_username(persona.username),
                    password_hash=PASSWORD_HASHER.hash(LOCAL_DEMO_PASSWORD),
                )
            )
        self.db.commit()

    @staticmethod
    def _invalid_credentials() -> AppError:
        return AppError(
            "账号或密码错误",
            code="LOCAL_LOGIN_INVALID",
            status=401,
        )
