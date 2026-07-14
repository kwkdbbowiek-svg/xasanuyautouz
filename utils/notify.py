"""
Centralized notification utilities.
- notify_super_admins: send error/alert to all super admins
- notify_relevant_admins: route payment/ad notifications to the right admin pool
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from config import settings
from database.connection import AsyncSessionFactory
from database.models import Admin, AdminRole

logger = logging.getLogger(__name__)

# Role → admin role mapping for routing
_ROLE_TO_ADMIN_ROLE: dict[str, AdminRole] = {
    "seller": AdminRole.seller_admin,
    "buyer": AdminRole.buyer_admin,
    "owner": AdminRole.owner_admin,
    "seeker": AdminRole.seeker_admin,
}

_SUB_TYPE_TO_ROLE: dict[str, str] = {
    "standard": "seller",
    "vip": "seller",
    "viewer": "seeker",   # FIX: viewer sub goes to seeker_admin, not buyer_admin
}


async def notify_super_admins(bot: Bot, text: str) -> None:
    """Send a plain text notification to all super admins."""
    for admin_id in settings.super_admin_ids:
        try:
            await bot.send_message(admin_id, f"⚠️ <b>Xatolik bildirnoması</b>\n\n{text}", parse_mode="HTML")
        except Exception as exc:
            logger.error("notify_super_admins error for %s: %s", admin_id, exc)


async def notify_relevant_admins(
    bot: Bot,
    sub_type: Optional[str],
    user_role: Optional[str],
    photo_file_id: Optional[str],
    document_file_id: Optional[str],
    caption: str,
    reply_markup: InlineKeyboardMarkup,
    is_video: bool = False,
) -> None:
    """
    Send notification to:
      1. Super admins (always)
      2. The domain assistant admin (seller/buyer/owner/seeker)

    sub_type is used for payment routing; user_role for ad routing.
    """
    # Determine which assistant admin role to notify
    target_role: Optional[AdminRole] = None
    if user_role and user_role in _ROLE_TO_ADMIN_ROLE:
        target_role = _ROLE_TO_ADMIN_ROLE[user_role]
    elif sub_type and sub_type in _SUB_TYPE_TO_ROLE:
        mapped = _SUB_TYPE_TO_ROLE[sub_type]
        target_role = _ROLE_TO_ADMIN_ROLE.get(mapped)

    # Collect recipient IDs (super admins + matching assistant admin)
    recipient_ids: set[int] = set(settings.super_admin_ids)

    if target_role:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Admin).where(
                    Admin.role == target_role,
                    Admin.is_active == True,  # noqa: E712
                )
            )
            assistant_admins = result.scalars().all()
        for adm in assistant_admins:
            recipient_ids.add(adm.telegram_id)

    # Deliver to each recipient
    for admin_id in recipient_ids:
        try:
            if photo_file_id and not is_video:
                await bot.send_photo(
                    admin_id,
                    photo=photo_file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            elif (document_file_id or photo_file_id) and is_video:
                fid = document_file_id or photo_file_id
                await bot.send_video(
                    admin_id,
                    video=fid,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            elif document_file_id and not is_video:
                await bot.send_document(
                    admin_id,
                    document=document_file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                await bot.send_message(
                    admin_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
        except Exception as exc:
            logger.error("notify_relevant_admins error for admin %s: %s", admin_id, exc)
