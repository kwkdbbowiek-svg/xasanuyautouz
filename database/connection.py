"""
Async SQLAlchemy engine, session factory, and DB initializer.
Features:
  - Auto-detects SQLite vs PostgreSQL from DATABASE_URL
  - Pool pre-ping keeps Railway's sleeping Postgres alive
  - Automatic reconnect on pool exhaustion / connection drop
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings
from database.models import Base

logger = logging.getLogger(__name__)

_is_sqlite: bool = settings.DATABASE_URL.startswith("sqlite")

# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────
if _is_sqlite:
    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    engine: AsyncEngine = create_async_engine(  # type: ignore[no-redef]
        settings.DATABASE_URL,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,          # keeps Railway Postgres alive
        pool_recycle=3600,           # recycle connections every 1 h
        pool_timeout=30,             # wait max 30 s for a free connection
    )

# ─────────────────────────────────────────────────────────────────────────────
# Session factory
# ─────────────────────────────────────────────────────────────────────────────
AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide a transactional async session.
    Commits on success, rolls back on any exception.
    Automatic retry on connection errors (OperationalError).
    """
    from sqlalchemy.exc import OperationalError
    import asyncio

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        async with AsyncSessionFactory() as session:
            try:
                yield session
                await session.commit()
                return
            except OperationalError as exc:
                await session.rollback()
                if attempt >= max_retries:
                    logger.error("DB connection failed after %d retries: %s", max_retries, exc)
                    raise
                wait = attempt * 2
                logger.warning(
                    "DB OperationalError (attempt %d/%d), retrying in %ds: %s",
                    attempt, max_retries, wait, exc
                )
                await asyncio.sleep(wait)
            except Exception:
                await session.rollback()
                raise


async def init_db() -> None:
    """Create all tables (idempotent — safe on every startup)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified successfully.")


async def close_db() -> None:
    """Gracefully dispose the connection pool on shutdown."""
    await engine.dispose()
    logger.info("Database connection pool closed.")


async def seed_default_settings() -> None:
    """Insert default settings rows if they don't already exist."""
    from sqlalchemy import select
    from database.models import Setting

    defaults = {
        "standard_price": str(settings.DEFAULT_STANDARD_PRICE),
        "vip_price": str(settings.DEFAULT_VIP_PRICE),
        "buyer_sub_price": str(settings.DEFAULT_BUYER_SUB_PRICE),
        "seeker_sub_price": str(settings.DEFAULT_SEEKER_SUB_PRICE),
        "standard_duration_days": str(settings.DEFAULT_STANDARD_DURATION_DAYS),
        "vip_duration_days": str(settings.DEFAULT_VIP_DURATION_DAYS),
        "buyer_sub_duration_days": str(settings.DEFAULT_BUYER_SUB_DURATION_DAYS),
        "seeker_sub_duration_days": str(settings.DEFAULT_SEEKER_SUB_DURATION_DAYS),
        "standard_ads_limit": str(settings.DEFAULT_STANDARD_ADS_LIMIT),
        "vip_ads_limit": str(settings.DEFAULT_VIP_ADS_LIMIT),
        "payment_card": "8600 0000 0000 0000",
        "payment_card_owner": "Adminov Admin",
    }

    async with AsyncSessionFactory() as session:
        for key, value in defaults.items():
            result = await session.execute(
                select(Setting).where(Setting.key == key)
            )
            if result.scalar_one_or_none() is None:
                session.add(Setting(key=key, value=value))
        await session.commit()
    logger.info("Default settings seeded.")
