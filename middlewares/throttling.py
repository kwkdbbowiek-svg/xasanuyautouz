"""
Anti-flood / throttling + WAF (Web Application Firewall) middleware.

Security layers:
  1. Rate-limiting: 1 req / THROTTLE_RATE seconds per user (in-memory)
  2. DDoS detection: 5+ req/s → 24-hour auto-ban stored in DB
  3. WAF: regex payload scanner – blocks SQL/HTML/JS/Python injection
  4. Media size guard: photo ≤ 5 MB, video ≤ 20 MB
  5. In-memory banned-ID cache: O(1) lookup, no DB hit per DDoS request
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User as TgUser

from config import settings
from database.connection import AsyncSessionFactory
from database.models import ThrottleLog, User

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory caches (survive for process lifetime)
# ─────────────────────────────────────────────────────────────────────────────
# {user_id: last_allowed_timestamp}
_last_seen: dict[int, float] = {}

# {user_id: violation_count}  (simple flood violations)
_violations: dict[int, int] = {}

# {user_id: deque of timestamps} for DDoS burst detection (sliding window)
_burst_window: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))

# {user_id: ban_expiry_timestamp} — hot in-memory ban cache
_banned_cache: dict[int, float] = {}

# One-time-warning set: don't spam the user with ban warnings
_warned: set[int] = set()

# ─────────────────────────────────────────────────────────────────────────────
# WAF — Malicious payload patterns
# ─────────────────────────────────────────────────────────────────────────────
_INJECTION_PATTERNS: list[re.Pattern] = [
    # SQL Injection
    re.compile(r"(union\s+select|drop\s+table|insert\s+into|delete\s+from|"
               r"update\s+\w+\s+set|or\s+1\s*=\s*1|'\s*or\s*'|--\s*$|"
               r";\s*drop|xp_cmdshell|exec\s*\(|cast\s*\(|convert\s*\()",
               re.IGNORECASE),
    # HTML / JS Injection
    re.compile(r"(<script|</script|javascript:|vbscript:|data:text/html|"
               r"onerror\s*=|onload\s*=|onclick\s*=|<iframe|<object|"
               r"<embed|<svg\s+onload|document\.cookie|window\.location)",
               re.IGNORECASE),
    # Python code injection
    re.compile(r"(__import__|__builtins__|__class__|__globals__|"
               r"\beval\s*\(|\bexec\s*\(|\bcompile\s*\(|"
               r"os\.system|subprocess\.|open\s*\(.*\bw\b)",
               re.IGNORECASE),
    # Path traversal
    re.compile(r"\.\./|\.\.\\|%2e%2e[%2f%5c]", re.IGNORECASE),
]

# Media size limits (bytes)
_PHOTO_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
_VIDEO_MAX_BYTES = 20 * 1024 * 1024  # 20 MB

# DDoS burst: more than N requests in 1 second → permanent 24h ban
_DDOS_BURST_COUNT = 5
_DDOS_BURST_WINDOW_SEC = 1.0
_DDOS_BAN_HOURS = 24


class ThrottlingMiddleware(BaseMiddleware):
    """Combined rate-limit + WAF middleware registered on the Dispatcher."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TgUser | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        user_id: int = user.id
        now_ts: float = time.monotonic()
        now_dt: datetime = datetime.now(timezone.utc)
        bot = data.get("bot")

        # ── 0. Hot in-memory ban check (O(1), no DB) ──────────────────────
        ban_exp = _banned_cache.get(user_id)
        if ban_exp and now_ts < ban_exp:
            await self._send_once(bot, event, user_id,
                                  "⛔ Siz vaqtincha bloklangansiz. Keyinroq urinib ko'ring.")
            return

        # ── 1. DB-persisted ban (survives restarts) ───────────────────────
        if ban_exp is None:  # not in hot cache → check DB once
            async with AsyncSessionFactory() as session:
                from sqlalchemy import select as sa_select
                res = await session.execute(
                    sa_select(ThrottleLog).where(ThrottleLog.user_id == user_id)
                )
                tlog = res.scalar_one_or_none()
                if tlog and tlog.banned_until and tlog.banned_until > now_dt:
                    exp_ts = now_ts + (tlog.banned_until - now_dt).total_seconds()
                    _banned_cache[user_id] = exp_ts
                    await self._send_once(bot, event, user_id,
                                          "⛔ Siz bloklangansiz. Keyinroq urinib ko'ring.")
                    return

        # ── 2. DDoS burst detection (sliding 1-second window) ─────────────
        window = _burst_window[user_id]
        window.append(now_ts)
        # Count requests within the last DDOS_BURST_WINDOW_SEC seconds
        cutoff = now_ts - _DDOS_BURST_WINDOW_SEC
        burst_count = sum(1 for t in window if t > cutoff)

        if burst_count >= _DDOS_BURST_COUNT:
            ban_until_dt = now_dt + timedelta(hours=_DDOS_BAN_HOURS)
            ban_until_ts = now_ts + _DDOS_BAN_HOURS * 3600
            _banned_cache[user_id] = ban_until_ts
            await self._persist_ban(user_id, ban_until_dt, now_dt, is_ddos=True)
            await self._send_once(bot, event, user_id,
                                  "⛔ DDoS hujumi aniqlandi. Siz 24 soatga bloklandi.")
            # Notify admins
            if bot:
                from utils.notify import notify_super_admins
                try:
                    username = user.username or "noma'lum"
                    await notify_super_admins(
                        bot,
                        f"🚨 DDoS hujumi aniqlandi!\n"
                        f"User ID: <code>{user_id}</code>\n"
                        f"Username: @{username}\n"
                        f"Burst: {burst_count} req/s → 24 soatga ban.",
                    )
                except Exception:
                    pass
            return

        # ── 3. Standard flood (1-per-THROTTLE_RATE) ───────────────────────
        last_ts = _last_seen.get(user_id, 0.0)
        elapsed = now_ts - last_ts

        if elapsed < settings.THROTTLE_RATE:
            _violations[user_id] = _violations.get(user_id, 0) + 1
            vcount = _violations[user_id]

            if vcount >= settings.THROTTLE_MAX_VIOLATIONS:
                ban_until_dt = now_dt + timedelta(seconds=settings.THROTTLE_BAN_DURATION)
                ban_until_ts = now_ts + settings.THROTTLE_BAN_DURATION
                _banned_cache[user_id] = ban_until_ts
                _violations.pop(user_id, None)
                await self._persist_ban(user_id, ban_until_dt, now_dt)
                await self._send_once(
                    bot, event, user_id,
                    f"⛔ Juda tez so'rov yubordingiz! "
                    f"{settings.THROTTLE_BAN_DURATION // 60} daqiqaga bloklandi."
                )
            # Always drop throttled updates silently
            return

        # ── 4. WAF — text payload scan ────────────────────────────────────
        if isinstance(event, Update):
            msg = event.message or event.edited_message
            if msg and msg.text:
                if self._is_malicious(msg.text):
                    await self._permanent_ban(user_id, now_dt, bot, user)
                    return

            # ── 5. Media size guard ───────────────────────────────────────
            if msg and msg.photo:
                largest = msg.photo[-1]
                if largest.file_size and largest.file_size > _PHOTO_MAX_BYTES:
                    if bot and msg:
                        try:
                            await bot.send_message(
                                user_id,
                                "⚠️ Rasm hajmi 5 MB dan oshmasligi kerak!"
                            )
                        except Exception:
                            pass
                    return

            if msg and msg.video:
                if msg.video.file_size and msg.video.file_size > _VIDEO_MAX_BYTES:
                    if bot and msg:
                        try:
                            await bot.send_message(
                                user_id,
                                "⚠️ Video hajmi 20 MB dan oshmasligi kerak!"
                            )
                        except Exception:
                            pass
                    return

        # ── 6. Allow — reset counters ─────────────────────────────────────
        _last_seen[user_id] = now_ts
        _violations.pop(user_id, None)
        _warned.discard(user_id)
        return await handler(event, data)

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _is_malicious(text: str) -> bool:
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                return True
        return False

    @staticmethod
    async def _send_once(bot, event, user_id: int, text: str) -> None:
        """Send a warning message only once per ban period."""
        if user_id in _warned:
            return
        _warned.add(user_id)
        if bot and isinstance(event, Update) and event.message:
            try:
                await bot.send_message(user_id, text)
            except Exception:
                pass

    @staticmethod
    async def _persist_ban(
        user_id: int, ban_until: datetime, now: datetime, is_ddos: bool = False
    ) -> None:
        from sqlalchemy import select as sa_select
        async with AsyncSessionFactory() as session:
            res = await session.execute(
                sa_select(ThrottleLog).where(ThrottleLog.user_id == user_id)
            )
            tlog = res.scalar_one_or_none()
            if tlog:
                tlog.banned_until = ban_until
                tlog.violation_count += 1
                tlog.last_request_at = now
            else:
                session.add(ThrottleLog(
                    user_id=user_id,
                    violation_count=1,
                    last_request_at=now,
                    banned_until=ban_until,
                ))
            await session.commit()
        logger.warning(
            "User %s %s-banned until %s",
            user_id, "DDoS" if is_ddos else "flood", ban_until
        )

    @staticmethod
    async def _permanent_ban(
        user_id: int, now: datetime, bot, tg_user: TgUser
    ) -> None:
        """Permanently ban a user who attempted injection and alert admins."""
        from sqlalchemy import select as sa_select
        # Mark in DB — 100-year ban = permanent
        ban_until = now + timedelta(days=365 * 100)
        async with AsyncSessionFactory() as session:
            # Also set user.is_banned = True
            u_res = await session.execute(
                sa_select(User).where(User.id == user_id)
            )
            user_row = u_res.scalar_one_or_none()
            if user_row:
                user_row.is_banned = True
                user_row.ban_until = ban_until

            res = await session.execute(
                sa_select(ThrottleLog).where(ThrottleLog.user_id == user_id)
            )
            tlog = res.scalar_one_or_none()
            if tlog:
                tlog.banned_until = ban_until
                tlog.violation_count += 999
            else:
                session.add(ThrottleLog(
                    user_id=user_id,
                    violation_count=999,
                    last_request_at=now,
                    banned_until=ban_until,
                ))
            await session.commit()

        # Hot cache
        _banned_cache[user_id] = time.monotonic() + 365 * 24 * 3600

        logger.critical(
            "INJECTION ATTEMPT — User %s (@%s) permanently banned.",
            user_id, tg_user.username or "no_username"
        )

        if bot:
            from utils.notify import notify_super_admins
            try:
                uname = tg_user.username or "noma'lum"
                await notify_super_admins(
                    bot,
                    f"🚨 <b>INJECTION HUJUMI aniqlandi!</b>\n\n"
                    f"👤 {tg_user.full_name}\n"
                    f"🆔 <code>{user_id}</code>\n"
                    f"Username: @{uname}\n\n"
                    f"Foydalanuvchi <b>abadiy</b> bloklandi."
                )
            except Exception:
                pass
