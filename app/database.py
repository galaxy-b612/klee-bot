"""Async SQLAlchemy database setup for Klee Core.

Uses aiosqlite as the async SQLite driver.
"""

import os
from pathlib import Path
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


# ---------------------------------------------------------------------------
# Default database URL
# ---------------------------------------------------------------------------

def _default_db_url() -> str:
    """Return the default SQLite database URL."""
    env_url = os.environ.get("KLEE_CORE_DATABASE_URL")
    if env_url:
        return env_url

    # Project-relative path: klee_core/data/klee.db
    current = Path(__file__).resolve().parent.parent  # klee_core/
    data_dir = current / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "klee.db"
    return f"sqlite+aiosqlite:///{db_path}"


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        db_url = _default_db_url()
        _engine = create_async_engine(db_url, echo=False)
    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:  # type: ignore
    """Yield an async database session (for FastAPI dependency injection)."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


async def init_db():
    """Create all tables. Call on app startup."""
    from app.models import Base  # noqa: PLC0415

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def reset_engine():
    """Reset the engine (useful for tests that swap databases)."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def override_db_url(url: str):
    """Override the database URL (for tests)."""
    os.environ["KLEE_CORE_DATABASE_URL"] = url
