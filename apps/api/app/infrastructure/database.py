from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def build_database(url: str):
    options = (
        {"connect_args": {"check_same_thread": False}}
        if url.startswith("sqlite")
        else {"pool_pre_ping": True}
    )
    if url.endswith(":memory:"):
        options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    elif engine.dialect.name == "postgresql":
        @event.listens_for(engine, "connect")
        def use_utc_for_postgresql(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.close()
    return engine, sessionmaker(engine, expire_on_commit=False)
