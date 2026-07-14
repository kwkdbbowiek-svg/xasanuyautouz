"""
Bot entry point.
  - Structured logging (console + rotating file)
  - DB init + seed
  - Throttling + WAF middleware
  - All routers registered
  - APScheduler for subscription expiry
  - Graceful shutdown (Railway SIGTERM safe)
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database.connection import close_db, init_db, seed_default_settings
from middlewares.throttling import ThrottlingMiddleware
from handlers import user, ad_posting, admin, broadcast
from utils.notify import notify_super_admins
from utils.scheduler import create_scheduler


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
def setup_logging() -> None:
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    formatter = logging.Formatter(log_format)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.WARNING)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # Enable DEBUG for our own modules to trace filter failures
    logging.getLogger("filters.admin_filters").setLevel(logging.DEBUG)
    logging.getLogger("handlers.admin").setLevel(logging.DEBUG)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────────
async def on_startup(bot: Bot) -> None:
    logger.info("Bot starting up...")
    logger.info("Super admin IDs from env: %s", settings.super_admin_ids)
    await init_db()
    await seed_default_settings()
    await notify_super_admins(bot, "🤖 Bot muvaffaqiyatli ishga tushdi!")
    logger.info("Startup complete.")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot shutting down — closing DB pool...")
    await close_db()
    logger.info("Graceful shutdown complete.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    setup_logging()

    bot = Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # ── Middleware ────────────────────────────────────────────────────────────
    dp.update.middleware(ThrottlingMiddleware())

    # ── Routers (order: admin first to capture /admin before generic handlers)
    dp.include_router(admin.router)
    dp.include_router(broadcast.router)
    dp.include_router(ad_posting.router)
    dp.include_router(user.router)

    # ── Lifecycle hooks ───────────────────────────────────────────────────────
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("APScheduler started.")

    try:
        logger.info("Starting polling...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
