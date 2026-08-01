from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...infrastructure.tables import GenerationLease, now


# This table-backed lease is the portability boundary. PostgreSQL/MySQL may
# replace the insert with ON CONFLICT/INSERT IGNORE, but callers must retain the
# owner token and expiry semantics instead of relying on a process-local lock.
GENERATION_LEASE_TTL = timedelta(minutes=15)


def acquire_generation_lease(db: Session, resource_key: str) -> str | None:
    """Acquire a cross-process generation lease, or return None if it is held."""

    current = now()
    db.execute(
        delete(GenerationLease).where(
            GenerationLease.resource_key == resource_key,
            GenerationLease.expires_at <= current,
        )
    )
    db.commit()
    owner_id = uuid4().hex
    db.add(
        GenerationLease(
            resource_key=resource_key,
            owner_id=owner_id,
            acquired_at=current,
            expires_at=current + GENERATION_LEASE_TTL,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return owner_id


def release_generation_lease(
    db: Session,
    resource_key: str,
    owner_id: str,
) -> None:
    """Release only the lease owned by this request."""

    db.rollback()
    db.execute(
        delete(GenerationLease).where(
            GenerationLease.resource_key == resource_key,
            GenerationLease.owner_id == owner_id,
        )
    )
    db.commit()


def renew_generation_lease(
    db: Session,
    resource_key: str,
    owner_id: str,
) -> bool:
    """Extend a live lease only while the caller still owns it.

    This is also the write fence used after slow provider calls. If another
    request has taken over an expired lease, the stale generator cannot renew
    and must not persist its result.
    """

    current = now()
    renewed = db.execute(
        update(GenerationLease)
        .where(
            GenerationLease.resource_key == resource_key,
            GenerationLease.owner_id == owner_id,
        )
        .values(expires_at=current + GENERATION_LEASE_TTL)
    )
    db.commit()
    return renewed.rowcount == 1
