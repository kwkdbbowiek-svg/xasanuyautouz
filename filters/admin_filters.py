"""
Custom Aiogram filters for admin role verification.
All checks hit the DB so they reflect live state (admin removed at runtime).
"""
from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select

from config import settings
from database.connection import AsyncSessionFactory
from database.models import Admin, AdminRole


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────
async def _get_admin(telegram_id: int) -> Admin | None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Admin).where(
                Admin.telegram_id == telegram_id,
                Admin.is_active == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# Filters
# ─────────────────────────────────────────────────────────────────────────────
class IsSuperAdmin(BaseFilter):
    """Passes only if the user is listed as super admin (env OR DB)."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id: int = event.from_user.id  # type: ignore[union-attr]

        # Check env-level super admins first (fastest)
        if user_id in settings.super_admin_ids:
            return True

        # Then check DB
        admin = await _get_admin(user_id)
        return admin is not None and admin.role == AdminRole.super_admin


class IsAnyAdmin(BaseFilter):
    """Passes if the user is any kind of admin (super or assistant)."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id: int = event.from_user.id  # type: ignore[union-attr]

        if user_id in settings.super_admin_ids:
            return True

        admin = await _get_admin(user_id)
        return admin is not None


class IsSellerAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id: int = event.from_user.id  # type: ignore[union-attr]
        if user_id in settings.super_admin_ids:
            return True
        admin = await _get_admin(user_id)
        return admin is not None and admin.role in (
            AdminRole.super_admin, AdminRole.seller_admin
        )


class IsBuyerAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id: int = event.from_user.id  # type: ignore[union-attr]
        if user_id in settings.super_admin_ids:
            return True
        admin = await _get_admin(user_id)
        return admin is not None and admin.role in (
            AdminRole.super_admin, AdminRole.buyer_admin
        )


class IsOwnerAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id: int = event.from_user.id  # type: ignore[union-attr]
        if user_id in settings.super_admin_ids:
            return True
        admin = await _get_admin(user_id)
        return admin is not None and admin.role in (
            AdminRole.super_admin, AdminRole.owner_admin
        )


class IsSeekerAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user_id: int = event.from_user.id  # type: ignore[union-attr]
        if user_id in settings.super_admin_ids:
            return True
        admin = await _get_admin(user_id)
        return admin is not None and admin.role in (
            AdminRole.super_admin, AdminRole.seeker_admin
        )
