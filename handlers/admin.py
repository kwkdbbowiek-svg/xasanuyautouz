"""
Admin panel handlers:
  - Super Admin: full control (pricing, blocks, admins, stats, Excel)
  - Assistant Admins: approve/reject payments and ads in their domain
  - Race condition protection: row-level locking with with_for_update()
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from sqlalchemy import select

from config import settings
from database.connection import AsyncSessionFactory, get_session
from database.models import (
    Ad, AdStatus, Admin, AdminRole, Block, Payment, PaymentStatus,
    Setting, Subscription, SubscriptionType, User, UserAdLimit, UserRole,
)
from filters.admin_filters import IsAnyAdmin, IsSuperAdmin
from utils.notify import notify_super_admins

logger = logging.getLogger(__name__)
router = Router(name="admin")


# ─────────────────────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────────────────────
class AdminStates(StatesGroup):
    # Settings
    set_standard_price = State()
    # Blocks
    add_block_name = State()
    # Admins
    add_admin_id = State()
    add_admin_role = State()
    # Reject reason
    reject_payment_reason = State()
    reject_ad_reason = State()
    # User limit management
    find_user_for_limit = State()
    set_extra_limit_amount = State()
    extend_sub_days = State()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _get_setting(key: str) -> str:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        s = result.scalar_one_or_none()
        return s.value if s else ""


async def _set_setting(key: str, value: str, admin_id: int) -> None:
    async with get_session() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        s = result.scalar_one_or_none()
        if s:
            s.value = value
            s.updated_by = admin_id
        else:
            session.add(Setting(key=key, value=value, updated_by=admin_id))


def _super_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="💰 Narxlarni o'zgartirish")],
            [KeyboardButton(text="🏘️ Bloklarni boshqarish"), KeyboardButton(text="👥 Adminlarni boshqarish")],
            [KeyboardButton(text="📢 Reklama yuborish"), KeyboardButton(text="📥 Excel yuklab olish")],
            [KeyboardButton(text="⏳ Kutilayotgan to'lovlar"), KeyboardButton(text="📋 Kutilayotgan e'lonlar")],
            [KeyboardButton(text="👤 Foydalanuvchi limitini o'zgartirish")],
        ],
        resize_keyboard=True,
    )


def _assistant_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Kutilayotgan to'lovlar")],
            [KeyboardButton(text="📋 Kutilayotgan e'lonlar")],
        ],
        resize_keyboard=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /admin entry
# ─────────────────────────────────────────────────────────────────────────────
@router.message(Command("admin"), IsAnyAdmin())
async def admin_panel(message: Message) -> None:
    try:
        is_super = message.from_user.id in settings.super_admin_ids
        if not is_super:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    select(Admin).where(Admin.telegram_id == message.from_user.id)
                )
                adm = result.scalar_one_or_none()
                is_super = adm and adm.role == AdminRole.super_admin

        kb = _super_admin_keyboard() if is_super else _assistant_admin_keyboard()
        await message.answer(
            f"🔐 Admin panelga xush kelibsiz, <b>{html.escape(message.from_user.full_name or '')}</b>!",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("admin_panel error")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📊 Statistika", IsSuperAdmin())
async def show_stats(message: Message) -> None:
    try:
        from sqlalchemy import func
        async with AsyncSessionFactory() as session:
            total_users = (await session.execute(select(func.count(User.id)))).scalar_one()
            sellers = (await session.execute(
                select(func.count(User.id)).where(User.role == UserRole.seller)
            )).scalar_one()
            buyers = (await session.execute(
                select(func.count(User.id)).where(User.role == UserRole.buyer)
            )).scalar_one()
            owners = (await session.execute(
                select(func.count(User.id)).where(User.role == UserRole.owner)
            )).scalar_one()
            seekers = (await session.execute(
                select(func.count(User.id)).where(User.role == UserRole.seeker)
            )).scalar_one()
            active_ads = (await session.execute(
                select(func.count(Ad.id)).where(Ad.status == AdStatus.active)
            )).scalar_one()
            pending_ads = (await session.execute(
                select(func.count(Ad.id)).where(Ad.status == AdStatus.pending)
            )).scalar_one()
            pending_pays = (await session.execute(
                select(func.count(Payment.id)).where(Payment.status == PaymentStatus.pending)
            )).scalar_one()

        text = (
            "📊 <b>Tizim statistikasi</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
            f"  🏠 Sotuvchilar: {sellers}\n"
            f"  🔍 Oluvchilar: {buyers}\n"
            f"  🏢 Kvartira egalari: {owners}\n"
            f"  🔑 Kvartira qidiruvchilar: {seekers}\n\n"
            f"📋 Faol e'lonlar: <b>{active_ads}</b>\n"
            f"⏳ Kutilayotgan e'lonlar: <b>{pending_ads}</b>\n"
            f"💳 Kutilayotgan to'lovlar: <b>{pending_pays}</b>"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as exc:
        logger.exception("show_stats error")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Pending Payments
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "⏳ Kutilayotgan to'lovlar", IsAnyAdmin())
async def pending_payments(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Payment).where(Payment.status == PaymentStatus.pending).order_by(Payment.created_at)
            )
            pays = result.scalars().all()

        if not pays:
            await message.answer("✅ Kutilayotgan to'lov yo'q.")
            return

        for pay in pays[:10]:  # Show 10 at a time
            async with AsyncSessionFactory() as session:
                user_res = await session.execute(select(User).where(User.id == pay.user_id))
                user = user_res.scalar_one_or_none()

            user_name = html.escape(user.full_name if user else "Noma'lum")
            sub_labels = {"standard": "Standart", "vip": "VIP", "viewer": "Ko'rish"}
            sub_label = sub_labels.get(pay.sub_type.value, pay.sub_type.value)

            caption = (
                f"💳 <b>To'lov #{pay.id}</b>\n"
                f"👤 {user_name} | <code>{pay.user_id}</code>\n"
                f"📦 Tur: {sub_label}\n"
                f"💰 Summa: {pay.amount:,} so'm\n"
                f"📅 Sana: {pay.created_at.strftime('%d.%m.%Y %H:%M')}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_approve:{pay.id}"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay_reject:{pay.id}"),
                ]
            ])
            try:
                await message.answer_photo(photo=pay.file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await message.answer(caption, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        logger.exception("pending_payments error")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Approve / Reject Payment  (Race-condition safe)
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("pay_approve:"), IsAnyAdmin())
async def approve_payment(call: CallbackQuery, bot: Bot) -> None:
    payment_id = int(call.data.split(":")[1])
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            # Row-level lock to prevent race conditions
            result = await session.execute(
                select(Payment).where(Payment.id == payment_id).with_for_update()
            )
            payment = result.scalar_one_or_none()

            if payment is None:
                await call.answer("To'lov topilmadi.", show_alert=True)
                return
            if payment.status != PaymentStatus.pending:
                status_labels = {
                    PaymentStatus.approved: "tasdiqlangan",
                    PaymentStatus.rejected: "rad etilgan",
                }
                lbl = status_labels.get(payment.status, payment.status.value)
                await call.answer(f"Bu to'lov allaqachon {lbl}!", show_alert=True)
                # Update the message to reflect state
                await call.message.edit_reply_markup(reply_markup=None)
                await call.message.edit_caption(
                    caption=call.message.caption + f"\n\n✅ Holat: {lbl.capitalize()} ({payment.reviewed_by})"
                    if call.message.caption else call.message.text + f"\n\n✅ Holat: {lbl.capitalize()}",
                    parse_mode="HTML",
                )
                return

            payment.status = PaymentStatus.approved
            payment.reviewed_by = call.from_user.id
            payment.reviewed_at = now

            # Calculate sub duration
            sub_type = payment.sub_type
            duration_key_map = {
                SubscriptionType.standard: "standard_duration_days",
                SubscriptionType.vip: "vip_duration_days",
                SubscriptionType.viewer: "buyer_sub_duration_days",
            }
            duration_key = duration_key_map.get(sub_type, "standard_duration_days")
            duration_result = await session.execute(
                select(Setting).where(Setting.key == duration_key)
            )
            dur_setting = duration_result.scalar_one_or_none()
            duration_days = int(dur_setting.value) if dur_setting else 30

            expires_at = now + timedelta(days=duration_days)
            sub = Subscription(
                user_id=payment.user_id,
                sub_type=sub_type,
                starts_at=now,
                expires_at=expires_at,
                is_active=True,
                payment_id=payment.id,
            )
            session.add(sub)
            await session.flush()

        # Update the inline keyboard in ALL admin messages to show reviewed state
        admin_name = html.escape(call.from_user.full_name or "Admin")
        new_caption = (
            (call.message.caption or call.message.text or "") +
            f"\n\n✅ <b>{admin_name}</b> tomonidan tasdiqlandi."
        )
        try:
            await call.message.edit_caption(caption=new_caption, parse_mode="HTML", reply_markup=None)
        except Exception:
            try:
                await call.message.edit_text(text=new_caption, parse_mode="HTML", reply_markup=None)
            except Exception:
                pass

        # Notify user
        sub_labels = {"standard": "Standart", "vip": "VIP", "viewer": "Ko'rish"}
        sub_label = sub_labels.get(payment.sub_type.value, payment.sub_type.value)
        try:
            await bot.send_message(
                payment.user_id,
                f"🎉 To'lovingiz tasdiqlandi!\n"
                f"📦 Obuna turi: <b>{sub_label}</b>\n"
                f"📅 Muddat: {expires_at.strftime('%d.%m.%Y')} gacha",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await call.answer("✅ To'lov tasdiqlandi!")

    except Exception as exc:
        logger.exception("approve_payment error id=%s", payment_id)
        await notify_super_admins(bot, f"To'lov tasdiqlash xatosi #{payment_id}: {exc}")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


@router.callback_query(F.data.startswith("pay_reject:"), IsAnyAdmin())
async def reject_payment_start(call: CallbackQuery, state: FSMContext) -> None:
    payment_id = int(call.data.split(":")[1])
    await state.update_data(rejecting_payment_id=payment_id, reject_msg_id=call.message.message_id)
    await state.set_state(AdminStates.reject_payment_reason)
    await call.message.answer("❌ Rad etish sababini kiriting:")
    await call.answer()


@router.message(AdminStates.reject_payment_reason, IsAnyAdmin(), F.text)
async def reject_payment_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    payment_id = data["rejecting_payment_id"]
    reason = html.escape(message.text.strip())
    await state.clear()
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(Payment).where(Payment.id == payment_id).with_for_update()
            )
            payment = result.scalar_one_or_none()
            if not payment or payment.status != PaymentStatus.pending:
                await message.answer("Bu to'lov allaqachon ko'rib chiqilgan.")
                return
            payment.status = PaymentStatus.rejected
            payment.reviewed_by = message.from_user.id
            payment.reviewed_at = now
            payment.reject_reason = reason

        await message.answer(f"✅ To'lov #{payment_id} rad etildi.")
        try:
            await bot.send_message(
                payment.user_id,
                f"❌ To'lovingiz rad etildi.\nSabab: {reason}\n\nIltimos, to'g'ri chek yuboring.",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("reject_payment_reason error")
        await notify_super_admins(bot, f"To'lov rad xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Pending Ads
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📋 Kutilayotgan e'lonlar", IsAnyAdmin())
async def pending_ads(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Ad).where(Ad.status == AdStatus.pending).order_by(Ad.created_at).limit(10)
            )
            ads = result.scalars().all()

        if not ads:
            await message.answer("✅ Kutilayotgan e'lon yo'q.")
            return

        for ad in ads:
            async with AsyncSessionFactory() as session:
                u_res = await session.execute(select(User).where(User.id == ad.owner_id))
                owner = u_res.scalar_one_or_none()
                b_res = await session.execute(select(Block).where(Block.id == ad.block_id))
                block = b_res.scalar_one_or_none()

            owner_name = html.escape(owner.full_name if owner else "Noma'lum")
            block_name = html.escape(block.name if block else "Noma'lum")
            badge = "⭐ VIP" if ad.sub_type == SubscriptionType.vip else "📦 Standart"
            ad_type_lbl = "Sotish" if ad.ad_type.value == "sale" else "Ijara"

            caption = (
                f"📋 <b>E'lon #{ad.id}</b> {badge}\n"
                f"👤 {owner_name} | <code>{ad.owner_id}</code>\n"
                f"🏘️ Blok: {block_name}\n"
                f"📌 {ad.title}\n"
                f"💰 {ad.price:,} so'm | {ad_type_lbl}\n"
                f"📝 {ad.description[:200]}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ad_approve:{ad.id}"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"ad_reject:{ad.id}"),
                ]
            ])
            try:
                if ad.media_file_id and ad.media_type == "photo":
                    await message.answer_photo(photo=ad.media_file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                elif ad.media_file_id and ad.media_type == "video":
                    await message.answer_video(video=ad.media_file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
                else:
                    await message.answer(caption, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await message.answer(caption, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        logger.exception("pending_ads error")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Approve / Reject Ad (Race-condition safe)
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ad_approve:"), IsAnyAdmin())
async def approve_ad(call: CallbackQuery, bot: Bot) -> None:
    ad_id = int(call.data.split(":")[1])
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(Ad).where(Ad.id == ad_id).with_for_update()
            )
            ad = result.scalar_one_or_none()
            if ad is None:
                await call.answer("E'lon topilmadi.", show_alert=True)
                return
            if ad.status != AdStatus.pending:
                await call.answer("Bu e'lon allaqachon ko'rib chiqilgan!", show_alert=True)
                try:
                    await call.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return
            ad.status = AdStatus.active
            ad.reviewed_by = call.from_user.id
            ad.reviewed_at = now

        admin_name = html.escape(call.from_user.full_name or "Admin")
        new_text = (
            (call.message.caption or call.message.text or "") +
            f"\n\n✅ <b>{admin_name}</b> tomonidan tasdiqlandi."
        )
        try:
            await call.message.edit_caption(caption=new_text, parse_mode="HTML", reply_markup=None)
        except Exception:
            try:
                await call.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=None)
            except Exception:
                pass

        try:
            await bot.send_message(ad.owner_id, f"🎉 E'loningiz #{ad_id} tasdiqlandi va e'lonlar taxtasida ko'rinadi!")
        except Exception:
            pass
        await call.answer("✅ E'lon tasdiqlandi!")

    except Exception as exc:
        logger.exception("approve_ad error id=%s", ad_id)
        await notify_super_admins(bot, f"E'lon tasdiqlash xatosi #{ad_id}: {exc}")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


@router.callback_query(F.data.startswith("ad_reject:"), IsAnyAdmin())
async def reject_ad_start(call: CallbackQuery, state: FSMContext) -> None:
    ad_id = int(call.data.split(":")[1])
    await state.update_data(rejecting_ad_id=ad_id)
    await state.set_state(AdminStates.reject_ad_reason)
    await call.message.answer("❌ Rad etish sababini kiriting:")
    await call.answer()


@router.message(AdminStates.reject_ad_reason, IsAnyAdmin(), F.text)
async def reject_ad_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    ad_id = data["rejecting_ad_id"]
    reason = html.escape(message.text.strip())
    await state.clear()
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(Ad).where(Ad.id == ad_id).with_for_update()
            )
            ad = result.scalar_one_or_none()
            if not ad or ad.status != AdStatus.pending:
                await message.answer("Bu e'lon allaqachon ko'rib chiqilgan.")
                return
            ad.status = AdStatus.rejected
            ad.reviewed_by = message.from_user.id
            ad.reviewed_at = now
            ad.reject_reason = reason

        await message.answer(f"✅ E'lon #{ad_id} rad etildi.")
        try:
            await bot.send_message(
                ad.owner_id,
                f"❌ E'loningiz #{ad_id} rad etildi.\nSabab: {reason}",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("reject_ad_reason error")
        await notify_super_admins(bot, f"E'lon rad xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Pricing Settings (Super Admin only)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "💰 Narxlarni o'zgartirish", IsSuperAdmin())
async def pricing_menu(message: Message) -> None:
    std = await _get_setting("standard_price")
    vip = await _get_setting("vip_price")
    buyer = await _get_setting("buyer_sub_price")
    seeker = await _get_setting("seeker_sub_price")
    std_days = await _get_setting("standard_duration_days")
    vip_days = await _get_setting("vip_duration_days")
    std_limit = await _get_setting("standard_ads_limit")
    vip_limit = await _get_setting("vip_ads_limit")
    card = await _get_setting("payment_card")

    text = (
        f"💰 <b>Joriy narxlar:</b>\n\n"
        f"📦 Standart: {std} so'm / {std_days} kun / {std_limit} ta e'lon\n"
        f"⭐ VIP: {vip} so'm / {vip_days} kun / {vip_limit} ta e'lon\n"
        f"🔍 Oluvchi obunasi: {buyer} so'm\n"
        f"🔑 Qidiruvchi obunasi: {seeker} so'm\n"
        f"💳 Karta: {card}\n\n"
        "Quyidagi tugmalardan birini bosing:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Standart narx", callback_data="setprice:standard_price")],
        [InlineKeyboardButton(text="⭐ VIP narx", callback_data="setprice:vip_price")],
        [InlineKeyboardButton(text="🔍 Oluvchi obuna narxi", callback_data="setprice:buyer_sub_price")],
        [InlineKeyboardButton(text="🔑 Qidiruvchi obuna narxi", callback_data="setprice:seeker_sub_price")],
        [InlineKeyboardButton(text="📅 Standart muddat (kun)", callback_data="setprice:standard_duration_days")],
        [InlineKeyboardButton(text="📅 VIP muddat (kun)", callback_data="setprice:vip_duration_days")],
        [InlineKeyboardButton(text="🔢 Standart e'lon limiti", callback_data="setprice:standard_ads_limit")],
        [InlineKeyboardButton(text="🔢 VIP e'lon limiti", callback_data="setprice:vip_ads_limit")],
        [InlineKeyboardButton(text="💳 Karta raqami", callback_data="setprice:payment_card")],
        [InlineKeyboardButton(text="👤 Karta egasi", callback_data="setprice:payment_card_owner")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


PRICE_SETTING_LABELS = {
    "standard_price": "Standart obuna narxi (so'm)",
    "vip_price": "VIP obuna narxi (so'm)",
    "buyer_sub_price": "Oluvchi obuna narxi (so'm)",
    "seeker_sub_price": "Qidiruvchi obuna narxi (so'm)",
    "standard_duration_days": "Standart obuna muddati (kun)",
    "vip_duration_days": "VIP obuna muddati (kun)",
    "standard_ads_limit": "Standart e'lon limiti",
    "vip_ads_limit": "VIP e'lon limiti",
    "payment_card": "To'lov karta raqami",
    "payment_card_owner": "Karta egasi ismi",
}


@router.callback_query(F.data.startswith("setprice:"), IsSuperAdmin())
async def setprice_callback(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[1]
    label = PRICE_SETTING_LABELS.get(key, key)
    await state.update_data(setting_key=key)
    await state.set_state(AdminStates.set_standard_price)
    await call.message.answer(f"✏️ Yangi qiymatni kiriting ({label}):")
    await call.answer()


@router.message(AdminStates.set_standard_price, IsSuperAdmin(), F.text)
async def handle_setting_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("setting_key", "")
    value = message.text.strip()

    numeric_keys = {
        "standard_price", "vip_price", "buyer_sub_price", "seeker_sub_price",
        "standard_duration_days", "vip_duration_days", "standard_ads_limit", "vip_ads_limit",
    }
    if key in numeric_keys:
        clean = value.replace(" ", "").replace(",", "")
        if not clean.isdigit():
            await message.answer("⚠️ Faqat raqam kiriting.")
            return
        value = clean

    await _set_setting(key, html.escape(value), message.from_user.id)
    await state.clear()
    label = PRICE_SETTING_LABELS.get(key, key)
    await message.answer(f"✅ <b>{label}</b> muvaffaqiyatli yangilandi: <code>{value}</code>", parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# Block Management (Super Admin only)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "🏘️ Bloklarni boshqarish", IsSuperAdmin())
async def manage_blocks(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(Block).order_by(Block.name))
            blocks = result.scalars().all()

        text = "🏘️ <b>Bloklarni boshqarish</b>\n\n"
        if blocks:
            for b in blocks:
                status = "✅" if b.is_active else "❌"
                text += f"{status} {b.id}. {html.escape(b.name)}\n"
        else:
            text += "Hali blok yo'q.\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Yangi blok qo'shish", callback_data="block_add")],
            *[
                [
                    InlineKeyboardButton(
                        text=("🔴 O'chir" if b.is_active else "🟢 Yoq") + f" — {b.name}",
                        callback_data=f"block_toggle:{b.id}"
                    )
                ]
                for b in blocks
            ],
        ])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        logger.exception("manage_blocks error")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data == "block_add", IsSuperAdmin())
async def block_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.add_block_name)
    await call.message.answer("➕ Yangi blok nomini kiriting (masalan: 1-blok):")
    await call.answer()


@router.message(AdminStates.add_block_name, IsSuperAdmin(), F.text)
async def block_add_name(message: Message, state: FSMContext) -> None:
    name = html.escape(message.text.strip())
    if len(name) < 2:
        await message.answer("⚠️ Blok nomi kamida 2 ta belgidan iborat bo'lishi kerak.")
        return
    await state.clear()
    try:
        async with get_session() as session:
            existing = await session.execute(select(Block).where(Block.name == name))
            if existing.scalar_one_or_none():
                await message.answer(f"⚠️ '{name}' nomli blok allaqachon mavjud.")
                return
            session.add(Block(name=name))
        await message.answer(f"✅ '{name}' bloki muvaffaqiyatli qo'shildi.")
    except Exception as exc:
        logger.exception("block_add_name error")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("block_toggle:"), IsSuperAdmin())
async def block_toggle(call: CallbackQuery) -> None:
    block_id = int(call.data.split(":")[1])
    try:
        async with get_session() as session:
            result = await session.execute(select(Block).where(Block.id == block_id).with_for_update())
            block = result.scalar_one_or_none()
            if not block:
                await call.answer("Blok topilmadi.", show_alert=True)
                return
            block.is_active = not block.is_active
            new_status = "faollashtirildi" if block.is_active else "o'chirildi"

        await call.answer(f"✅ Blok {new_status}!")
        await call.message.delete()
        # Refresh the block list
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(Block).order_by(Block.name))
            blocks = result.scalars().all()

        text = "🏘️ <b>Bloklarni boshqarish</b>\n\n"
        for b in blocks:
            status = "✅" if b.is_active else "❌"
            text += f"{status} {b.id}. {html.escape(b.name)}\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Yangi blok qo'shish", callback_data="block_add")],
            *[
                [InlineKeyboardButton(
                    text=("🔴 O'chir" if b.is_active else "🟢 Yoq") + f" — {b.name}",
                    callback_data=f"block_toggle:{b.id}"
                )]
                for b in blocks
            ],
        ])
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        logger.exception("block_toggle error")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# Admin Management (Super Admin only)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "👥 Adminlarni boshqarish", IsSuperAdmin())
async def manage_admins(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Admin).where(Admin.is_active == True).order_by(Admin.created_at)  # noqa: E712
            )
            admins = result.scalars().all()

        text = "👥 <b>Yordamchi adminlar:</b>\n\n"
        if admins:
            for a in admins:
                text += f"• {html.escape(a.full_name)} | <code>{a.telegram_id}</code> — {a.role.value}\n"
        else:
            text += "Hali yordamchi admin yo'q.\n"

        role_buttons = [
            [InlineKeyboardButton(text="➕ Seller Admin", callback_data="addadmin:seller_admin")],
            [InlineKeyboardButton(text="➕ Buyer Admin", callback_data="addadmin:buyer_admin")],
            [InlineKeyboardButton(text="➕ Owner Admin", callback_data="addadmin:owner_admin")],
            [InlineKeyboardButton(text="➕ Seeker Admin", callback_data="addadmin:seeker_admin")],
        ]
        if admins:
            role_buttons += [
                [InlineKeyboardButton(
                    text=f"🗑️ {a.full_name} ({a.role.value})",
                    callback_data=f"deladmin:{a.telegram_id}"
                )]
                for a in admins
            ]

        kb = InlineKeyboardMarkup(inline_keyboard=role_buttons)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        logger.exception("manage_admins error")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("addadmin:"), IsSuperAdmin())
async def addadmin_start(call: CallbackQuery, state: FSMContext) -> None:
    role_str = call.data.split(":")[1]
    await state.update_data(new_admin_role=role_str)
    await state.set_state(AdminStates.add_admin_id)
    await call.message.answer(
        f"👤 <b>{role_str}</b> uchun admin Telegram ID sini kiriting:\n"
        "(Foydalanuvchi botga /start bosgan bo'lishi kerak)",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.add_admin_id, IsSuperAdmin(), F.text)
async def addadmin_id(message: Message, state: FSMContext) -> None:
    id_str = message.text.strip()
    if not id_str.lstrip("-").isdigit():
        await message.answer("⚠️ Telegram ID faqat raqamlardan iborat bo'lishi kerak.")
        return
    new_id = int(id_str)
    data = await state.get_data()
    role_str = data["new_admin_role"]
    await state.clear()

    try:
        async with AsyncSessionFactory() as session:
            u_res = await session.execute(select(User).where(User.id == new_id))
            user = u_res.scalar_one_or_none()
            full_name = user.full_name if user else f"Admin_{new_id}"

        async with get_session() as session:
            existing = await session.execute(
                select(Admin).where(Admin.telegram_id == new_id)
            )
            adm = existing.scalar_one_or_none()
            if adm:
                adm.role = AdminRole(role_str)
                adm.is_active = True
                adm.full_name = full_name
            else:
                session.add(Admin(
                    telegram_id=new_id,
                    full_name=full_name,
                    role=AdminRole(role_str),
                    added_by=message.from_user.id,
                    is_active=True,
                ))

        await message.answer(
            f"✅ <code>{new_id}</code> foydalanuvchisi <b>{role_str}</b> sifatida tayinlandi.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("addadmin_id error")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("deladmin:"), IsSuperAdmin())
async def delete_admin(call: CallbackQuery) -> None:
    admin_tid = int(call.data.split(":")[1])
    try:
        async with get_session() as session:
            result = await session.execute(
                select(Admin).where(Admin.telegram_id == admin_tid).with_for_update()
            )
            adm = result.scalar_one_or_none()
            if adm:
                adm.is_active = False
        await call.answer(f"✅ Admin {admin_tid} o'chirildi!")
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.exception("delete_admin error")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# Excel export shortcut from admin panel
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📥 Excel yuklab olish", IsSuperAdmin())
async def admin_excel(message: Message) -> None:
    from utils.excel_exporter import export_users_to_excel
    try:
        await message.answer("⏳ Excel fayl tayyorlanmoqda...")
        file_path = await export_users_to_excel()
        from aiogram.types import FSInputFile
        doc = FSInputFile(file_path, filename="uysavdo_users.xlsx")
        await message.answer_document(doc, caption="📥 Foydalanuvchilar ro'yxati")
        import os
        os.remove(file_path)
    except Exception as exc:
        logger.exception("admin_excel error")
        await message.answer("⚠️ Excel fayl yaratishda xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# User Limit & Subscription Management (Super Admin only)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "👤 Foydalanuvchi limitini o'zgartirish", IsSuperAdmin())
async def user_limit_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.find_user_for_limit)
    await message.answer(
        "🔍 Foydalanuvchining Telegram ID yoki @username ni kiriting:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
            resize_keyboard=True,
        ),
    )


@router.message(AdminStates.find_user_for_limit, IsSuperAdmin(), F.text)
async def find_user_for_limit(message: Message, state: FSMContext, bot: Bot) -> None:
    from sqlalchemy import func as sqlfunc
    if message.text == "❌ Bekor qilish":
        await state.clear()
        from aiogram.types import ReplyKeyboardRemove
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return

    query_text = message.text.strip().lstrip("@")
    now = datetime.now(timezone.utc)

    try:
        async with AsyncSessionFactory() as session:
            # Try by numeric ID first, then by username
            user = None
            if query_text.isdigit():
                res = await session.execute(
                    select(User).where(User.id == int(query_text))
                )
                user = res.scalar_one_or_none()
            if user is None:
                res = await session.execute(
                    select(User).where(User.username == query_text)
                )
                user = res.scalar_one_or_none()

        if not user:
            await message.answer(
                "❌ Foydalanuvchi topilmadi. Telegram ID yoki @username ni tekshiring."
            )
            return

        # Fetch active subscription
        async with AsyncSessionFactory() as session:
            sub_res = await session.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == user.id,
                    Subscription.sub_type.in_([
                        SubscriptionType.standard, SubscriptionType.vip
                    ]),
                    Subscription.is_active == True,   # noqa: E712
                    Subscription.expires_at > now,
                )
                .order_by(Subscription.expires_at.desc())
            )
            active_sub = sub_res.scalars().first()

            # Current override
            ov_res = await session.execute(
                select(UserAdLimit).where(UserAdLimit.user_id == user.id)
            )
            override = ov_res.scalar_one_or_none()

            # Current active ad count
            count_res = await session.execute(
                select(sqlfunc.count(Ad.id)).where(
                    Ad.owner_id == user.id,
                    Ad.status.in_([AdStatus.active, AdStatus.pending]),
                )
            )
            active_ad_count = count_res.scalar_one()

        role_labels = {
            "seller": "🏠 Sotuvchi", "buyer": "🔍 Oluvchi",
            "owner": "🏢 Kvartira egasi", "seeker": "🔑 Kvartira qidiruvchi",
        }
        role_str = role_labels.get(user.role.value if user.role else "", "—")
        sub_info = "Faol obuna yo'q"
        base_limit = 0
        if active_sub:
            base_limit_key = "vip_ads_limit" if active_sub.sub_type == SubscriptionType.vip else "standard_ads_limit"
            base_limit = int(await _get_setting(base_limit_key))
            sub_info = (
                f"{active_sub.sub_type.value.upper()} — "
                f"{active_sub.expires_at.strftime('%d.%m.%Y')} gacha"
            )

        extra = override.extra_limit if override else 0
        total_limit = base_limit + extra

        info_text = (
            f"👤 <b>{html.escape(user.full_name)}</b>\n"
            f"🆔 <code>{user.id}</code>\n"
            f"Rol: {role_str}\n"
            f"Obuna: {sub_info}\n"
            f"📊 Asosiy limit: {base_limit} | Qo'shimcha: +{extra} | Jami: {total_limit}\n"
            f"📋 Hozirgi e'lonlar: {active_ad_count}/{total_limit}"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Limit qo'shish", callback_data=f"ulimit_add:{user.id}"),
                InlineKeyboardButton(text="0⃣ Limitni nolga", callback_data=f"ulimit_zero:{user.id}"),
            ],
            [
                InlineKeyboardButton(text="📅 Obunani uzaytirish", callback_data=f"ulimit_extend:{user.id}"),
            ],
        ])

        await state.update_data(target_user_id=user.id, target_user_name=user.full_name)
        await state.clear()  # clear state but keep the inline buttons active
        await message.answer(info_text, parse_mode="HTML", reply_markup=kb)

    except Exception as exc:
        logger.exception("find_user_for_limit error")
        await notify_super_admins(bot, f"Limit qidirish xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ── Inline: Add extra limit ───────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ulimit_add:"), IsSuperAdmin())
async def ulimit_add_start(call: CallbackQuery, state: FSMContext) -> None:
    user_id = int(call.data.split(":")[1])
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminStates.set_extra_limit_amount)
    await call.message.answer("➕ Qo'shiladigan limit sonini kiriting (masalan: 5 yoki 10):")
    await call.answer()


@router.message(AdminStates.set_extra_limit_amount, IsSuperAdmin(), F.text)
async def set_extra_limit_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user_id = data["target_user_id"]
    raw = message.text.strip()
    if not raw.lstrip("+").isdigit():
        await message.answer("⚠️ Faqat musbat raqam kiriting.")
        return
    extra = int(raw.lstrip("+"))
    await state.clear()

    try:
        async with get_session() as session:
            ov_res = await session.execute(
                select(UserAdLimit).where(UserAdLimit.user_id == user_id).with_for_update()
            )
            override = ov_res.scalar_one_or_none()
            if override:
                override.extra_limit += extra
                override.set_by = message.from_user.id
            else:
                session.add(UserAdLimit(
                    user_id=user_id,
                    extra_limit=extra,
                    set_by=message.from_user.id,
                ))

        await message.answer(
            f"✅ Foydalanuvchi <code>{user_id}</code> ga +{extra} ta qo'shimcha limit berildi.",
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                user_id,
                f"🎉 Admin sizga <b>+{extra}</b> ta qo'shimcha e'lon limiti berdi!\n"
                f"Endi ko'proq e'lon bera olasiz.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("set_extra_limit_amount error")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ── Inline: Zero out limit ────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ulimit_zero:"), IsSuperAdmin())
async def ulimit_zero(call: CallbackQuery, bot: Bot) -> None:
    user_id = int(call.data.split(":")[1])
    try:
        async with get_session() as session:
            ov_res = await session.execute(
                select(UserAdLimit).where(UserAdLimit.user_id == user_id).with_for_update()
            )
            override = ov_res.scalar_one_or_none()
            if override:
                override.extra_limit = 0
                override.set_by = call.from_user.id
            else:
                session.add(UserAdLimit(user_id=user_id, extra_limit=0, set_by=call.from_user.id))

        await call.answer("✅ Qo'shimcha limit nolga tushurildi!")
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(
            f"✅ Foydalanuvchi <code>{user_id}</code> ning qo'shimcha limiti nolga tushurildi.",
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                user_id,
                "ℹ️ Adminlar tomonidan qo'shimcha e'lon limitingiz nolga tushirildi."
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("ulimit_zero error")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


# ── Inline: Extend subscription ───────────────────────────────────────────────
@router.callback_query(F.data.startswith("ulimit_extend:"), IsSuperAdmin())
async def ulimit_extend_start(call: CallbackQuery, state: FSMContext) -> None:
    user_id = int(call.data.split(":")[1])
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminStates.extend_sub_days)
    await call.message.answer(
        "📅 Obunani necha kun uzaytirmoqchisiz? (masalan: 30):"
    )
    await call.answer()


@router.message(AdminStates.extend_sub_days, IsSuperAdmin(), F.text)
async def extend_sub_days(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user_id = data["target_user_id"]
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("⚠️ Musbat raqam kiriting.")
        return
    days = int(raw)
    await state.clear()
    now = datetime.now(timezone.utc)

    try:
        async with get_session() as session:
            sub_res = await session.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.sub_type.in_([SubscriptionType.standard, SubscriptionType.vip]),
                    Subscription.is_active == True,   # noqa: E712
                )
                .order_by(Subscription.expires_at.desc())
                .with_for_update()
            )
            sub = sub_res.scalars().first()

            if sub:
                # If already expired, extend from now; otherwise from current expiry
                base_dt = max(sub.expires_at, now)
                sub.expires_at = base_dt + timedelta(days=days)
                sub.is_active = True
                new_expiry = sub.expires_at
            else:
                # Create a new free subscription
                new_expiry = now + timedelta(days=days)
                session.add(Subscription(
                    user_id=user_id,
                    sub_type=SubscriptionType.standard,
                    starts_at=now,
                    expires_at=new_expiry,
                    is_active=True,
                ))

        await message.answer(
            f"✅ Foydalanuvchi <code>{user_id}</code> obunasi "
            f"<b>{days} kun</b> uzaytirildi.\n"
            f"Yangi tugash sanasi: <b>{new_expiry.strftime('%d.%m.%Y')}</b>",
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                user_id,
                f"🎉 Admin tomonidan obunangiz <b>{days} kun</b> uzaytirildi!\n"
                f"📅 Yangi tugash sanasi: <b>{new_expiry.strftime('%d.%m.%Y')}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("extend_sub_days error")
        await notify_super_admins(bot, f"Obuna uzaytirish xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Admin Management (Super Admin only)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "👥 Adminlarni boshqarish", IsSuperAdmin())
async def manage_admins(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Admin).where(Admin.is_active == True).order_by(Admin.created_at)  # noqa: E712
            )
            admins = result.scalars().all()

        text = "👥 <b>Yordamchi adminlar:</b>\n\n"
        if admins:
            for a in admins:
                text += f"• {html.escape(a.full_name)} | <code>{a.telegram_id}</code> — {a.role.value}\n"
        else:
            text += "Hali yordamchi admin yo'q.\n"

        role_buttons = [
            [InlineKeyboardButton(text="➕ Seller Admin", callback_data="addadmin:seller_admin")],
            [InlineKeyboardButton(text="➕ Buyer Admin", callback_data="addadmin:buyer_admin")],
            [InlineKeyboardButton(text="➕ Owner Admin", callback_data="addadmin:owner_admin")],
            [InlineKeyboardButton(text="➕ Seeker Admin", callback_data="addadmin:seeker_admin")],
        ]
        if admins:
            role_buttons += [
                [InlineKeyboardButton(
                    text=f"🗑️ {a.full_name} ({a.role.value})",
                    callback_data=f"deladmin:{a.telegram_id}"
                )]
                for a in admins
            ]

        kb = InlineKeyboardMarkup(inline_keyboard=role_buttons)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        logger.exception("manage_admins error")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("addadmin:"), IsSuperAdmin())
async def addadmin_start(call: CallbackQuery, state: FSMContext) -> None:
    role_str = call.data.split(":")[1]
    await state.update_data(new_admin_role=role_str)
    await state.set_state(AdminStates.add_admin_id)
    await call.message.answer(
        f"👤 <b>{role_str}</b> uchun admin Telegram ID sini kiriting:",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.add_admin_id, IsSuperAdmin(), F.text)
async def addadmin_id(message: Message, state: FSMContext) -> None:
    id_str = message.text.strip()
    if not id_str.lstrip("-").isdigit():
        await message.answer("⚠️ Telegram ID faqat raqamlardan iborat bo'lishi kerak.")
        return
    new_id = int(id_str)
    data = await state.get_data()
    role_str = data["new_admin_role"]
    await state.clear()
    try:
        async with AsyncSessionFactory() as session:
            u_res = await session.execute(select(User).where(User.id == new_id))
            user = u_res.scalar_one_or_none()
            full_name = user.full_name if user else f"Admin_{new_id}"

        async with get_session() as session:
            existing = await session.execute(select(Admin).where(Admin.telegram_id == new_id))
            adm = existing.scalar_one_or_none()
            if adm:
                adm.role = AdminRole(role_str)
                adm.is_active = True
                adm.full_name = full_name
            else:
                session.add(Admin(
                    telegram_id=new_id,
                    full_name=full_name,
                    role=AdminRole(role_str),
                    added_by=message.from_user.id,
                    is_active=True,
                ))

        await message.answer(
            f"✅ <code>{new_id}</code> foydalanuvchisi <b>{role_str}</b> sifatida tayinlandi.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("addadmin_id error")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("deladmin:"), IsSuperAdmin())
async def delete_admin(call: CallbackQuery) -> None:
    admin_tid = int(call.data.split(":")[1])
    try:
        async with get_session() as session:
            result = await session.execute(
                select(Admin).where(Admin.telegram_id == admin_tid).with_for_update()
            )
            adm = result.scalar_one_or_none()
            if adm:
                adm.is_active = False
        await call.answer(f"✅ Admin {admin_tid} o'chirildi!")
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.exception("delete_admin error")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# Excel export shortcut from admin panel
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📥 Excel yuklab olish", IsSuperAdmin())
async def admin_excel(message: Message) -> None:
    from utils.excel_exporter import export_users_to_excel
    import os
    try:
        await message.answer("⏳ Excel fayl tayyorlanmoqda...")
        file_path = await export_users_to_excel()
        from aiogram.types import FSInputFile
        doc = FSInputFile(file_path, filename="uysavdo_users.xlsx")
        await message.answer_document(doc, caption="📥 Foydalanuvchilar ro'yxati")
        os.remove(file_path)
    except Exception as exc:
        logger.exception("admin_excel error")
        await message.answer("⚠️ Excel fayl yaratishda xatolik yuz berdi.")
