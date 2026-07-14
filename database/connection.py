"""
Async SQLAlchemy engine, session factory, and DB initializer.
BUG FIX: get_session() retry loop removed — yield inside a for-loop
in asynccontextmanager is invalid (generator can only yield once).
Replaced with a flat context manager + single retry on OperationalError.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from config import settings
from database.models import Base

logger = logging.getLogger(__name__)

_is_sqlite: bool = settings.async_database_url.startswith("sqlite")

if _is_sqlite:
    engine: AsyncEngine = create_async_engine(
        settings.async_database_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    engine: AsyncEngine = create_async_engine(  # type: ignore[no-redef]
        settings.async_database_url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_timeout=30,
    )

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
    Transactional session. Commits on success, rolls back on any exception.
    Single retry on OperationalError (lost connection).
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except OperationalError as exc:
            await session.rollback()
            logger.warning("DB OperationalError, retrying once: %s", exc)
            # Wait briefly then retry the whole transaction
            await asyncio.sleep(2)
            async with AsyncSessionFactory() as session2:
                try:
                    yield session2          # type: ignore[misc]
                    await session2.commit()
                except Exception:
                    await session2.rollback()
                    raise
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified successfully.")


async def close_db() -> None:
    await engine.dispose()
    logger.info("Database connection pool closed.")


async def seed_default_settings() -> None:
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
            result = await session.execute(select(Setting).where(Setting.key == key))
            if result.scalar_one_or_none() is None:
                session.add(Setting(key=key, value=value))
        await session.commit()
    logger.info("Default settings seeded.")
