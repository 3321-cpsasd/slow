from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import AlphaRegistrationQuota, now


def _quota_insert_statement(*, dialect_name: str, quota_date: str, daily_limit: int):
    if dialect_name == "postgresql":
        insert_statement = postgresql_insert(AlphaRegistrationQuota)
    elif dialect_name == "sqlite":
        insert_statement = sqlite_insert(AlphaRegistrationQuota)
    else:
        raise RuntimeError(
            f"Alpha registration quota does not support {dialect_name}"
        )
    return (
        insert_statement
        .values(
            quota_date=quota_date,
            used_count=0,
            limit_snapshot=daily_limit,
            updated_at=now(),
        )
        .on_conflict_do_nothing(index_elements=["quota_date"])
    )


def claim_alpha_registration(
    db: Session,
    *,
    quota_date: str,
    daily_limit: int,
) -> None:
    """Atomically reserve one Alpha self-registration in the caller's transaction."""

    db.execute(
        _quota_insert_statement(
            dialect_name=db.get_bind().dialect.name,
            quota_date=quota_date,
            daily_limit=daily_limit,
        )
    )

    claimed = db.execute(
        update(AlphaRegistrationQuota)
        .where(
            AlphaRegistrationQuota.quota_date == quota_date,
            AlphaRegistrationQuota.used_count < daily_limit,
        )
        .values(
            used_count=AlphaRegistrationQuota.used_count + 1,
            limit_snapshot=daily_limit,
            updated_at=now(),
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise AppError(
            "今天的 Alpha 名额已满，请明天再试",
            code="REGISTRATION_DAILY_LIMIT_REACHED",
            status=429,
        )
