from datetime import timedelta

from app.infrastructure.database import build_database
from app.infrastructure.tables import Base, GenerationLease, now
from app.modules.learning.generation_leases import (
    acquire_generation_lease,
    release_generation_lease,
    renew_generation_lease,
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


def test_stale_generation_owner_cannot_renew_after_takeover(tmp_path):
    engine, sessions = build_database(
        f"sqlite+pysqlite:///{tmp_path / 'stale-generation.db'}"
    )
    Base.metadata.create_all(engine)

    with sessions() as first, sessions() as second:
        stale_owner = acquire_generation_lease(first, "section:stale")
        lease = first.get(GenerationLease, "section:stale")
        lease.expires_at = now() - timedelta(seconds=1)
        first.commit()

        current_owner = acquire_generation_lease(second, "section:stale")
        assert current_owner and current_owner != stale_owner
        assert renew_generation_lease(
            first,
            "section:stale",
            stale_owner,
        ) is False
        assert renew_generation_lease(
            second,
            "section:stale",
            current_owner,
        ) is True
