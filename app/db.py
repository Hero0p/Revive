"""SQLite, one file, tables created on startup. No migrations tool."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL
from app.models import Base

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
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
