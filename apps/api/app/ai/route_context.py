from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class InvocationRouteContext:
    purpose: str
    authority: str
    deployment_id: str
    model_family_id: str
    config_version_id: str
    route_policy_version: str
    fallback_index: int


_current_route: ContextVar[InvocationRouteContext | None] = ContextVar(
    "ai_invocation_route",
    default=None,
)


def current_route_context() -> InvocationRouteContext | None:
    return _current_route.get()


@contextmanager
def invocation_route_context(context: InvocationRouteContext):
    token = _current_route.set(context)
    try:
        yield
    finally:
        _current_route.reset(token)
