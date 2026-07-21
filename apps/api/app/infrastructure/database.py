from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def build_database(url: str):
    options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    if url.endswith(":memory:"):
        options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    return engine, sessionmaker(engine, expire_on_commit=False)
