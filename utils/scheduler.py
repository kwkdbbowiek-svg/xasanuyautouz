"""
APScheduler background tasks.
- expire_subscriptions: runs every hour, marks expired subscriptions inactive
  and sets related ads to 'expired' status.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from config import settings
from database.connection import AsyncSessionFactory
from database.models import Ad, AdStatus, Subscription

logger = logging.getLogger(__name__)


async def expire_subscriptions() -> None:
    """
    1. Find subscriptions that have passed their expires_at date.
    2. Mark them is_active = False.
    3. Mark the owner's ads as 'expired' so they disappear from listings.
    """
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionFactory() as session:
            # Find expired but still-active subscriptions
            result = await session.execute(
                select(Subscription).where(
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.expires_at <= now,
                )
            )
            expired_subs = result.scalars().all()

            if not expired_subs:
                return

            expired_user_ids = []
            for sub in expired_subs:
                sub.is_active = False
                expired_user_ids.append(sub.user_id)

            # Expire ads for users whose subscription ended
            if expired_user_ids:
                await session.execute(
                    update(Ad)
                    .where(
                        Ad.owner_id.in_(expired_user_ids),
                        Ad.status == AdStatus.active,
                    )
                    .values(status=AdStatus.expired)
                )

            await session.commit()
            logger.info(
                "Scheduler: expired %d subscriptions and related ads for users: %s",
                len(expired_subs),
                expired_user_ids,
            )
    except Exception as exc:
        logger.exception("expire_subscriptions scheduler error: %s", exc)


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        expire_subscriptions,
        trigger="interval",
        hours=settings.SCHEDULER_INTERVAL_HOURS,
        id="expire_subscriptions",
        replace_existing=True,
    )
    return scheduler
