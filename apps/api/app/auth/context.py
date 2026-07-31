from dataclasses import dataclass
from typing import Literal

from ..core.errors import AppError


ActorKind = Literal["user", "system_worker"]


@dataclass(frozen=True)
class Principal:
    """A verified actor and the user whose data the operation may affect."""

    actor_kind: ActorKind
    actor_id: str
    subject_user_id: str
    session_id: str | None


@dataclass(frozen=True)
class UserScope:
    """A request scope that can only represent an authenticated end user."""

    principal: Principal

    def __post_init__(self):
        if self.principal.actor_kind != "user":
            raise AppError(
                "系统执行者不能使用用户请求上下文",
                code="USER_SCOPE_REQUIRED",
                status=403,
            )
        if self.principal.actor_id != self.principal.subject_user_id:
            raise AppError(
                "用户执行者与数据主体不一致",
                code="USER_SCOPE_SUBJECT_MISMATCH",
                status=403,
            )

    @property
    def user_id(self) -> str:
        return self.principal.subject_user_id


@dataclass(frozen=True)
class WorkerExecutionContext:
    """A fenced system execution scoped to one durable user task."""

    principal: Principal
    task_id: str
    lease_owner: str
    lease_token: str

    def __post_init__(self):
        if self.principal.actor_kind != "system_worker":
            raise AppError(
                "后台任务必须使用系统执行上下文",
                code="WORKER_SCOPE_REQUIRED",
                status=403,
            )
        if self.principal.actor_id != self.lease_owner:
            raise AppError(
                "Worker 身份与租约所有者不一致",
                code="WORKER_LEASE_OWNER_MISMATCH",
                status=403,
            )

    @property
    def user_id(self) -> str:
        return self.principal.subject_user_id


def demo_user_scope(user_id: str) -> UserScope:
    return UserScope(
        Principal(
            actor_kind="user",
            actor_id=user_id,
            subject_user_id=user_id,
            session_id=None,
        )
    )
