"""
Configuration module - loads all environment variables via pydantic-settings.
"""
from pydantic_settings import BaseSettings
from pydantic import SecretStr
from functools import lru_cache


class Settings(BaseSettings):
    # ── Bot ──────────────────────────────────────────────────────────────────
    BOT_TOKEN: SecretStr
    SUPER_ADMIN_IDS: str = ""          # comma-separated telegram user IDs

    # ── Database ─────────────────────────────────────────────────────────────
    # Railway injects DATABASE_URL as postgresql:// — we auto-fix to asyncpg
    DATABASE_URL: str = "sqlite+aiosqlite:///uysavdouz.db"  # fallback for local

    @property
    def async_database_url(self) -> str:
        """Convert Railway's postgres:// or postgresql:// to asyncpg driver URL."""
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    # ── Security ─────────────────────────────────────────────────────────────
    THROTTLE_RATE: float = 1.2         # seconds between allowed messages
    THROTTLE_BAN_DURATION: int = 900   # 15 minutes in seconds
    THROTTLE_MAX_VIOLATIONS: int = 5   # violations before auto-ban

    # ── Scheduler ────────────────────────────────────────────────────────────
    SCHEDULER_INTERVAL_HOURS: int = 1

    # ── Logging ──────────────────────────────────────────────────────────────
    LOG_FILE: str = "bot_errors.log"

    # ── Defaults (overridable by super admin at runtime) ─────────────────────
    DEFAULT_STANDARD_PRICE: int = 50_000
    DEFAULT_VIP_PRICE: int = 150_000
    DEFAULT_BUYER_SUB_PRICE: int = 30_000
    DEFAULT_SEEKER_SUB_PRICE: int = 30_000
    DEFAULT_STANDARD_DURATION_DAYS: int = 30
    DEFAULT_VIP_DURATION_DAYS: int = 30
    DEFAULT_BUYER_SUB_DURATION_DAYS: int = 30
    DEFAULT_SEEKER_SUB_DURATION_DAYS: int = 30
    DEFAULT_STANDARD_ADS_LIMIT: int = 3
    DEFAULT_VIP_ADS_LIMIT: int = 10

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def super_admin_ids(self) -> list[int]:
        """Return parsed list of super admin telegram IDs."""
        if not self.SUPER_ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.SUPER_ADMIN_IDS.split(",") if x.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
