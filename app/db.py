"""SQLite, one file, tables created on startup. No migrations tool."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL
from app.models import Base

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL plus synchronous=NORMAL.

    A comparison run commits once per simulated tick -- thousands of small
    transactions -- and the default rollback journal fsyncs on every one of
    them. WAL is the standard fix and does not trade away integrity: under WAL,
    synchronous=NORMAL risks losing only the most recent transaction on a power
    cut, never a corrupt database.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_all() -> None:
    Base.metadata.create_all(engine)


def get_session():
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def reset_database() -> None:
    """Used by the demo reset endpoint and the tests."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
