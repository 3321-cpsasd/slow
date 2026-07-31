from datetime import timedelta

from app.infrastructure.database import build_database
from app.infrastructure.tables import Base, GenerationLease, now
from app.modules.learning.generation_leases import (
    acquire_generation_lease,
    release_generation_lease,
)


def test_generation_lease_is_exclusive_and_owner_scoped(tmp_path):
    engine, sessions = build_database(
        f"sqlite+pysqlite:///{tmp_path / 'leases.db'}"
    )
    Base.metadata.create_all(engine)

    with sessions() as first, sessions() as second:
        owner = acquire_generation_lease(first, "chapter:one")
        assert owner
        assert acquire_generation_lease(second, "chapter:one") is None

        release_generation_lease(second, "chapter:one", "another-owner")
        assert acquire_generation_lease(second, "chapter:one") is None

        release_generation_lease(first, "chapter:one", owner)
        replacement = acquire_generation_lease(second, "chapter:one")
        assert replacement and replacement != owner


def test_expired_generation_lease_can_be_recovered(tmp_path):
    engine, sessions = build_database(
        f"sqlite+pysqlite:///{tmp_path / 'expired-leases.db'}"
    )
    Base.metadata.create_all(engine)

    with sessions() as db:
        db.add(
            GenerationLease(
                resource_key="section:one",
                owner_id="interrupted-worker",
                acquired_at=now() - timedelta(minutes=20),
                expires_at=now() - timedelta(minutes=5),
            )
        )
        db.commit()
        assert acquire_generation_lease(db, "section:one")
