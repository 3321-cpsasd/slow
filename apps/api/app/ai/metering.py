import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from ..auth.context import Principal
from ..infrastructure.tables import AiInvocation, AiUsageMeasurement, now


_current_principal: ContextVar[Principal | None] = ContextVar(
    "ai_usage_principal",
    default=None,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _value(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    result = {}
    for name in (
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens_details",
        "prompt_tokens_details",
        "output_tokens_details",
        "completion_tokens_details",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_creation",
    ):
        item = getattr(value, name, None)
        if item is not None:
            result[name] = _as_dict(item) if not isinstance(item, (str, int, float, bool)) else item
    return result


@dataclass(frozen=True)
class NormalizedUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict | None = None


def normalize_openai_usage(usage) -> NormalizedUsage | None:
    if usage is None:
        return None
    input_details = _value(usage, "input_tokens_details") or _value(
        usage, "prompt_tokens_details", {}
    )
    output_details = _value(usage, "output_tokens_details") or _value(
        usage, "completion_tokens_details", {}
    )
    input_tokens = _value(usage, "input_tokens")
    if input_tokens is None:
        input_tokens = _value(usage, "prompt_tokens")
    output_tokens = _value(usage, "output_tokens")
    if output_tokens is None:
        output_tokens = _value(usage, "completion_tokens")
    total = _value(usage, "total_tokens")
    if total is None and input_tokens is not None and output_tokens is not None:
        total = int(input_tokens) + int(output_tokens)
    return NormalizedUsage(
        input_tokens=int(input_tokens) if input_tokens is not None else None,
        cached_input_tokens=int(_value(input_details, "cached_tokens", 0) or 0),
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        reasoning_tokens=int(_value(output_details, "reasoning_tokens", 0) or 0),
        total_tokens=int(total) if total is not None else None,
        raw=_as_dict(usage),
    )


def normalize_anthropic_usage(usage) -> NormalizedUsage | None:
    if not usage:
        return None
    creation = _value(usage, "cache_creation", {}) or {}
    uncached = int(_value(usage, "input_tokens", 0) or 0)
    cache_read = int(_value(usage, "cache_read_input_tokens", 0) or 0)
    cache_create = int(_value(usage, "cache_creation_input_tokens", 0) or 0)
    write_5m = int(_value(creation, "ephemeral_5m_input_tokens", cache_create) or 0)
    write_1h = int(_value(creation, "ephemeral_1h_input_tokens", 0) or 0)
    output = int(_value(usage, "output_tokens", 0) or 0)
    return NormalizedUsage(
        input_tokens=uncached,
        cached_input_tokens=cache_read,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        output_tokens=output,
        total_tokens=uncached + cache_read + write_5m + write_1h + output,
        raw=_as_dict(usage),
    )


class AiUsageRecorder:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    @contextmanager
    def attributed(self, principal: Principal):
        token = _current_principal.set(principal)
        try:
            yield
        finally:
            _current_principal.reset(token)

    def start(
        self,
        *,
        provider: str,
        api_mode: str,
        model: str,
        operation: str,
        attribution_status: str = "legacy_unverified",
    ) -> str:
        invocation_id = _uid("aiinv")
        principal = _current_principal.get()
        if principal:
            attribution_status = "verified"
        with self.sessions() as db:
            db.add(
                AiInvocation(
                    id=invocation_id,
                    provider=provider,
                    api_mode=api_mode,
                    model=model,
                    operation=operation,
                    status="started",
                    usage_status="pending",
                    attribution_status=attribution_status,
                    actor_kind=(
                        principal.actor_kind
                        if principal
                        else "system"
                        if attribution_status == "system"
                        else ""
                    ),
                    actor_id=principal.actor_id if principal else "",
                    subject_user_id=(
                        principal.subject_user_id if principal else None
                    ),
                )
            )
            db.commit()
        return invocation_id

    def succeed(
        self,
        invocation_id: str,
        usage: NormalizedUsage | None,
        *,
        provider_response_id: str = "",
    ) -> None:
        self._finish(
            invocation_id,
            status="succeeded",
            usage=usage,
            provider_response_id=provider_response_id,
        )

    def fail(self, invocation_id: str, error: BaseException) -> None:
        self._finish(
            invocation_id,
            status="interrupted" if isinstance(error, asyncio.CancelledError) else "failed",
            usage=None,
            error_code=getattr(error, "code", type(error).__name__)[:80],
        )

    def _finish(
        self,
        invocation_id: str,
        *,
        status: str,
        usage: NormalizedUsage | None,
        provider_response_id: str = "",
        error_code: str = "",
    ) -> None:
        finished = now()
        with self.sessions() as db:
            invocation = db.get(AiInvocation, invocation_id)
            if not invocation or invocation.status != "started":
                return
            started = invocation.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            invocation.status = status
            invocation.usage_status = "reported" if usage else "missing"
            invocation.provider_response_id = provider_response_id or ""
            invocation.error_code = error_code
            invocation.finished_at = finished
            invocation.latency_ms = max(
                0, int((finished - started).total_seconds() * 1000)
            )
            if usage:
                payload = asdict(usage)
                raw = payload.pop("raw") or {}
                db.add(
                    AiUsageMeasurement(
                        id=_uid("aiusage"),
                        invocation_id=invocation_id,
                        source="provider_response",
                        quality="reported",
                        raw_usage_json=json.dumps(raw, ensure_ascii=False),
                        **payload,
                    )
                )
            db.commit()


class NullAiUsageRecorder:
    def start(self, **_kwargs) -> None:
        return None

    def succeed(self, _invocation_id, _usage, **_kwargs) -> None:
        return None

    def fail(self, _invocation_id, _error) -> None:
        return None
