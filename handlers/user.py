"""
User-facing handlers:
  - /start → role selection
  - Subscription purchase flow (check submission)
  - Block selection & paginated ad browsing
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import select, func

from database.connection import AsyncSessionFactory, get_session
from database.models import (
    Ad, AdMedia, AdStatus, AdType, Block, Payment, PaymentStatus,
    Setting, Subscription, SubscriptionType, User, UserRole,
)
from utils.notify import notify_super_admins

logger = logging.getLogger(__name__)
router = Router(name="user")

ADS_PER_PAGE = 5


# ─────────────────────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────────────────────
class UserStates(StatesGroup):
    waiting_role = State()
    waiting_payment_check = State()   # awaiting receipt photo
    browsing_ads = State()            # paginated browsing


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _get_setting(key: str) -> str:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        s = result.scalar_one_or_none()
        return s.value if s else ""


async def _upsert_user(tg_user) -> User:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(User).where(User.id == tg_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                id=tg_user.id,
                username=tg_user.username,
                full_name=html.escape(tg_user.full_name or ""),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def _active_subscription(user_id: int, sub_types: list[SubscriptionType]) -> Subscription | None:
    """
    FIX: Use .scalars().first() — user can have multiple historical rows.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.sub_type.in_(sub_types),
                Subscription.is_active == True,  # noqa: E712
                Subscription.expires_at > now,
            ).order_by(Subscription.expires_at.desc())
        )
        return result.scalars().first()   # ← NOT scalar_one_or_none()


def _role_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Sotuvchi"), KeyboardButton(text="🔍 Oluvchi")],
            [KeyboardButton(text="🏢 Kvartira egasi"), KeyboardButton(text="🔑 Kvartira qidirayotgan")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        user = await _upsert_user(message.from_user)
        if user.role:
            await _show_main_menu(message, user)
        else:
            await message.answer(
                "👋 Assalomu alaykum! <b>Shirin shahri Uy va Kvartira Bozori</b>ga xush kelibsiz!\n\n"
                "Iltimos, rolingizni tanlang:",
                reply_markup=_role_keyboard(),
                parse_mode="HTML",
            )
            await state.set_state(UserStates.waiting_role)
    except Exception as exc:
        logger.exception("cmd_start error for user %s", message.from_user.id)
        await notify_super_admins(message.bot, f"/start xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")


async def _show_main_menu(message: Message, user: User) -> None:
    role_labels = {
        UserRole.seller: "🏠 Sotuvchi",
        UserRole.buyer: "🔍 Oluvchi",
        UserRole.owner: "🏢 Kvartira egasi",
        UserRole.seeker: "🔑 Kvartira qidirayotgan",
    }
    label = role_labels.get(user.role, "Foydalanuvchi")
    buttons = []
    if user.role in (UserRole.seller, UserRole.owner):
        buttons = [
            [KeyboardButton(text="📋 E'lon berish"), KeyboardButton(text="📊 Mening e'lonlarim")],
            [KeyboardButton(text="💳 Obuna sotib olish"), KeyboardButton(text="ℹ️ Ma'lumot")],
        ]
    else:
        buttons = [
            [KeyboardButton(text="🏘️ Bloklarni ko'rish")],
            [KeyboardButton(text="💳 Obuna sotib olish"), KeyboardButton(text="ℹ️ Ma'lumot")],
        ]
    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    await message.answer(
        f"✅ Xush kelibsiz, <b>{html.escape(user.full_name)}</b>!\nRolingiz: <b>{label}</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Role selection
# ─────────────────────────────────────────────────────────────────────────────
ROLE_MAP = {
    "🏠 Sotuvchi": UserRole.seller,
    "🔍 Oluvchi": UserRole.buyer,
    "🏢 Kvartira egasi": UserRole.owner,
    "🔑 Kvartira qidirayotgan": UserRole.seeker,
}


@router.message(UserStates.waiting_role, F.text.in_(ROLE_MAP.keys()))
async def handle_role_selection(message: Message, state: FSMContext) -> None:
    try:
        role = ROLE_MAP[message.text]
        async with get_session() as session:
            result = await session.execute(select(User).where(User.id == message.from_user.id))
            user = result.scalar_one_or_none()
            if user:
                user.role = role
        await state.clear()
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(User).where(User.id == message.from_user.id))
            user = result.scalar_one_or_none()
        await _show_main_menu(message, user)
    except Exception as exc:
        logger.exception("handle_role_selection error")
        await notify_super_admins(message.bot, f"Rol tanlash xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.message(UserStates.waiting_role)
async def handle_invalid_role(message: Message) -> None:
    await message.answer("❌ Iltimos, tugmalardan birini tanlang.", reply_markup=_role_keyboard())


# ─────────────────────────────────────────────────────────────────────────────
# Subscription purchase
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "💳 Obuna sotib olish")
async def subscription_menu(message: Message, state: FSMContext) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(User).where(User.id == message.from_user.id))
            user = result.scalar_one_or_none()
        if not user or not user.role:
            await message.answer("Avval /start buyrug'ini bosing.")
            return

        card = await _get_setting("payment_card")
        card_owner = await _get_setting("payment_card_owner")

        if user.role == UserRole.seller:
            std_price = await _get_setting("standard_price")
            vip_price = await _get_setting("vip_price")
            std_days = await _get_setting("standard_duration_days")
            vip_days = await _get_setting("vip_duration_days")
            std_limit = await _get_setting("standard_ads_limit")
            vip_limit = await _get_setting("vip_ads_limit")
            text = (
                "🏠 <b>Sotuvchi obuna narxlari:</b>\n\n"
                f"📦 <b>Standart:</b> {std_price} so'm / {std_days} kun\n"
                f"   • E'lonlar soni: {std_limit} ta\n\n"
                f"⭐ <b>VIP:</b> {vip_price} so'm / {vip_days} kun\n"
                f"   • E'lonlar soni: {vip_limit} ta\n"
                f"   • E'lonlaringiz eng tepada ko'rsatiladi!\n\n"
                f"💳 To'lov kartasi: <code>{card}</code>\n"
                f"👤 Karta egasi: {card_owner}\n\n"
                "To'lovni amalga oshirib, <b>chek rasmini</b> yuboring."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Standart obuna", callback_data="sub_buy:standard")],
                [InlineKeyboardButton(text="⭐ VIP obuna", callback_data="sub_buy:vip")],
            ])
        elif user.role == UserRole.owner:
            std_price = await _get_setting("standard_price")
            vip_price = await _get_setting("vip_price")
            std_days = await _get_setting("standard_duration_days")
            vip_days = await _get_setting("vip_duration_days")
            text = (
                "🏢 <b>Kvartira egasi obuna narxlari:</b>\n\n"
                f"📦 <b>Standart:</b> {std_price} so'm / {std_days} kun\n"
                f"⭐ <b>VIP:</b> {vip_price} so'm / {vip_days} kun\n\n"
                f"💳 To'lov kartasi: <code>{card}</code>\n"
                f"👤 Karta egasi: {card_owner}\n\n"
                "To'lovni amalga oshirib, <b>chek rasmini</b> yuboring."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Standart obuna", callback_data="sub_buy:standard")],
                [InlineKeyboardButton(text="⭐ VIP obuna", callback_data="sub_buy:vip")],
            ])
        elif user.role == UserRole.buyer:
            price = await _get_setting("buyer_sub_price")
            days = await _get_setting("buyer_sub_duration_days")
            text = (
                "🔍 <b>Oluvchi obunasi:</b>\n\n"
                f"💰 Narxi: {price} so'm / {days} kun\n"
                f"✅ Obuna orqali barcha bloklardagi e'lonlarni ko'rishingiz mumkin.\n\n"
                f"💳 To'lov kartasi: <code>{card}</code>\n"
                f"👤 Karta egasi: {card_owner}\n\n"
                "To'lovni amalga oshirib, <b>chek rasmini</b> yuboring."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👁️ Ko'rish obunasi", callback_data="sub_buy:viewer")],
            ])
        else:  # seeker
            price = await _get_setting("seeker_sub_price")
            days = await _get_setting("seeker_sub_duration_days")
            text = (
                "🔑 <b>Kvartira qidirayotgan obunasi:</b>\n\n"
                f"💰 Narxi: {price} so'm / {days} kun\n"
                f"✅ Obuna orqali barcha bloklardagi ijaraga berish e'lonlarini ko'rishingiz mumkin.\n\n"
                f"💳 To'lov kartasi: <code>{card}</code>\n"
                f"👤 Karta egasi: {card_owner}\n\n"
                "To'lovni amalga oshirib, <b>chek rasmini</b> yuboring."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👁️ Ko'rish obunasi", callback_data="sub_buy:viewer")],
            ])

        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        logger.exception("subscription_menu error")
        await notify_super_admins(message.bot, f"Obuna menu xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("sub_buy:"))
async def sub_buy_callback(call: CallbackQuery, state: FSMContext) -> None:
    try:
        sub_type_str = call.data.split(":")[1]
        await state.update_data(pending_sub_type=sub_type_str)
        await state.set_state(UserStates.waiting_payment_check)
        await call.message.answer(
            "📸 Iltimos, to'lov cheki rasmini (screenshot yoki foto) yuboring:",
            reply_markup=ReplyKeyboardRemove(),
        )
        await call.answer()
    except Exception as exc:
        logger.exception("sub_buy_callback error")
        await call.answer("Xatolik yuz berdi", show_alert=True)


@router.message(UserStates.waiting_payment_check, F.photo | F.document)
async def receive_payment_check(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        data = await state.get_data()
        sub_type_str = data.get("pending_sub_type", "standard")

        # Extract file info
        if message.photo:
            file = message.photo[-1]
            file_id = file.file_id
            file_unique_id = file.file_unique_id
        else:
            file_id = message.document.file_id
            file_unique_id = message.document.file_unique_id

        # ── Fraud check: has this exact receipt been used before? ──────────
        async with AsyncSessionFactory() as session:
            existing = await session.execute(
                select(Payment).where(Payment.file_unique_id == file_unique_id)
            )
            if existing.scalar_one_or_none():
                await message.answer(
                    "⛔ Bu chek avval ham yuborilgan! Iltimos, haqiqiy to'lov chekini yuboring.\n"
                    "Takroriy urinishlar avtomatik bloklanadi."
                )
                await state.clear()
                return

        # Determine amount from settings
        price_key = {
            "standard": "standard_price",
            "vip": "vip_price",
            "viewer": "buyer_sub_price",
        }.get(sub_type_str, "standard_price")
        amount = int(await _get_setting(price_key) or "0")

        # Save payment as pending
        sub_type_enum = SubscriptionType(sub_type_str)
        async with get_session() as session:
            payment = Payment(
                user_id=message.from_user.id,
                sub_type=sub_type_enum,
                file_unique_id=file_unique_id,
                file_id=file_id,
                amount=amount,
                status=PaymentStatus.pending,
            )
            session.add(payment)
            await session.flush()
            payment_id = payment.id

        await state.clear()
        await message.answer(
            "✅ Chekingiz qabul qilindi! Admin tekshirgandan so'ng obunangiz faollashadi.\n"
            "Odatda 5-30 daqiqa ichida tasdiqlanadi."
        )

        # Notify relevant admins
        user = message.from_user
        role_text = {
            "standard": "Standart e'lon",
            "vip": "VIP e'lon",
            "viewer": "Ko'rish obunasi",
        }.get(sub_type_str, sub_type_str)

        caption = (
            f"💳 <b>Yangi to'lov cheki</b>\n\n"
            f"👤 Foydalanuvchi: {html.escape(user.full_name or '')}\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"📋 Obuna turi: {role_text}\n"
            f"💰 Summa: {amount:,} so'm\n"
            f"🔢 To'lov ID: #{payment_id}"
        )
        approve_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay_approve:{payment_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay_reject:{payment_id}"),
            ]
        ])

        from utils.notify import notify_relevant_admins
        await notify_relevant_admins(
            bot=bot,
            sub_type=sub_type_str,
            user_role=None,
            photo_file_id=file_id if message.photo else None,
            document_file_id=file_id if message.document else None,
            caption=caption,
            reply_markup=approve_kb,
        )

    except Exception as exc:
        logger.exception("receive_payment_check error")
        await notify_super_admins(message.bot, f"Chek qabul xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")
        await state.clear()


@router.message(UserStates.waiting_payment_check)
async def invalid_payment_message(message: Message) -> None:
    await message.answer("📸 Iltimos, faqat rasm yoki fayl yuboring (to'lov cheki).")


# ─────────────────────────────────────────────────────────────────────────────
# Block selection & paginated ad browsing
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "🏘️ Bloklarni ko'rish")
async def show_blocks(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(User).where(User.id == message.from_user.id))
            user = result.scalar_one_or_none()

        if not user or user.role not in (UserRole.buyer, UserRole.seeker):
            await message.answer("Bu funksiya faqat Oluvchi va Kvartira qidirayotganlar uchun.")
            return

        # Check active subscription
        sub_types = [SubscriptionType.viewer]
        sub = await _active_subscription(user.id, sub_types)
        if not sub:
            await message.answer(
                "🔒 E'lonlarni ko'rish uchun obuna kerak.\n"
                "💳 <b>Obuna sotib olish</b> tugmasini bosing.",
                parse_mode="HTML",
            )
            return

        # Fetch active blocks
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Block).where(Block.is_active == True).order_by(Block.name)  # noqa: E712
            )
            blocks = result.scalars().all()

        if not blocks:
            await message.answer("Hozircha hech qanday blok yo'q.")
            return

        buttons = [
            [InlineKeyboardButton(text=f"🏘️ {b.name}", callback_data=f"block:{b.id}:0")]
            for b in blocks
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer("Qaysi blokdagi e'lonlarni ko'rmoqchisiz?", reply_markup=kb)

    except Exception as exc:
        logger.exception("show_blocks error")
        await notify_super_admins(message.bot, f"Bloklar xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(F.data.startswith("block:"))
async def show_ads_in_block(call: CallbackQuery) -> None:
    try:
        _, block_id_str, page_str = call.data.split(":")
        block_id = int(block_id_str)
        page = int(page_str)

        # Subscription re-check on every page view
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(User).where(User.id == call.from_user.id))
            user = result.scalar_one_or_none()

        if not user:
            await call.answer("Foydalanuvchi topilmadi.", show_alert=True)
            return

        sub = await _active_subscription(user.id, [SubscriptionType.viewer])
        if not sub:
            await call.answer("Obunangiz tugagan yoki yo'q!", show_alert=True)
            return

        async with AsyncSessionFactory() as session:
            block_res = await session.execute(select(Block).where(Block.id == block_id))
            block = block_res.scalar_one_or_none()
            if not block:
                await call.answer("Blok topilmadi.", show_alert=True)
                return

            # Determine ad_type filter by user role
            ad_type_filter = AdType.sale if user.role == UserRole.buyer else AdType.rent

            # Total count for pagination
            count_res = await session.execute(
                select(func.count(Ad.id)).where(
                    Ad.block_id == block_id,
                    Ad.status == AdStatus.active,
                    Ad.ad_type == ad_type_filter,
                )
            )
            total = count_res.scalar_one()

            # VIP first, then standard — paginated
            from sqlalchemy import case as sa_case
            ads_res = await session.execute(
                select(Ad)
                .where(
                    Ad.block_id == block_id,
                    Ad.status == AdStatus.active,
                    Ad.ad_type == ad_type_filter,
                )
                .order_by(
                    sa_case(
                        (Ad.sub_type == SubscriptionType.vip, 0), else_=1
                    ),
                    Ad.created_at.desc(),
                )
                .offset(page * ADS_PER_PAGE)
                .limit(ADS_PER_PAGE)
            )
            ads = ads_res.scalars().all()

        if not ads:
            await call.message.answer(f"🏘️ <b>{html.escape(block.name)}</b> blokida e'lon yo'q.", parse_mode="HTML")
            await call.answer()
            return

        await call.message.answer(
            f"🏘️ <b>{html.escape(block.name)}</b> — {total} ta e'lon\n"
            f"📄 Sahifa: {page + 1}/{(total + ADS_PER_PAGE - 1) // ADS_PER_PAGE}",
            parse_mode="HTML",
        )

        for ad in ads:
            await _send_single_ad(call.message, ad)

        # Pagination buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"block:{block_id}:{page - 1}")
            )
        if (page + 1) * ADS_PER_PAGE < total:
            nav_buttons.append(
                InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"block:{block_id}:{page + 1}")
            )
        if nav_buttons:
            await call.message.answer(
                "Navigatsiya:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
            )

        await call.answer()

    except Exception as exc:
        logger.exception("show_ads_in_block error")
        await notify_super_admins(call.bot, f"E'lonlar ko'rish xatosi: {exc}")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


async def _send_single_ad(message: Message, ad: Ad) -> None:
    """Send one ad card. Loads AdMedia from DB and sends as album if multiple photos."""
    from aiogram.types import InputMediaPhoto, InputMediaVideo

    # Load media for this ad
    async with AsyncSessionFactory() as session:
        media_res = await session.execute(
            select(AdMedia).where(AdMedia.ad_id == ad.id).order_by(AdMedia.sort_order)
        )
        media_files = media_res.scalars().all()

    badge = "⭐ VIP" if ad.sub_type == SubscriptionType.vip else "📦 Standart"
    ad_type_label = "🏠 Sotiladi" if ad.ad_type == AdType.sale else "🏢 Ijaraga beriladi"
    text = (
        f"{badge} | {ad_type_label}\n\n"
        f"📌 <b>{html.escape(ad.title)}</b>\n"
        f"📝 {html.escape(ad.description)}\n"
        f"💰 Narx: <b>{ad.price:,} so'm</b>\n"
    )
    if ad.rooms:
        text += f"🚪 Xonalar: {ad.rooms} ta\n"
    if ad.floor and ad.total_floors:
        text += f"🏢 Qavat: {ad.floor}/{ad.total_floors}\n"
    if ad.area:
        text += f"📐 Maydon: {ad.area} m²\n"
    if ad.contact_phone:
        text += f"📞 Tel: {html.escape(ad.contact_phone)}\n"

    photos = [m for m in media_files if m.media_type.value == "photo"]
    videos = [m for m in media_files if m.media_type.value == "video"]

    try:
        if len(photos) > 1:
            # Send as media group (album)
            media_group = [
                InputMediaPhoto(
                    media=photos[0].file_id,
                    caption=text,
                    parse_mode="HTML",
                )
            ] + [InputMediaPhoto(media=p.file_id) for p in photos[1:]]
            await message.answer_media_group(media=media_group)
        elif len(photos) == 1:
            await message.answer_photo(
                photo=photos[0].file_id, caption=text, parse_mode="HTML"
            )
        elif videos:
            await message.answer_video(
                video=videos[0].file_id, caption=text, parse_mode="HTML"
            )
        else:
            await message.answer(text, parse_mode="HTML")
    except Exception:
        await message.answer(text, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# My Ads
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📊 Mening e'lonlarim")
async def my_ads(message: Message) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Ad).where(Ad.owner_id == message.from_user.id).order_by(Ad.created_at.desc())
            )
            ads = result.scalars().all()

        if not ads:
            await message.answer("Sizda hali e'lon yo'q.")
            return

        text = "📊 <b>Mening e'lonlarim:</b>\n\n"
        for i, ad in enumerate(ads, 1):
            status_emoji = {
                "pending": "⏳", "active": "✅", "rejected": "❌",
                "expired": "⌛", "deleted": "🗑️",
            }.get(ad.status.value, "❓")
            text += f"{i}. {status_emoji} {html.escape(ad.title)} — {ad.price:,} so'm\n"

        await message.answer(text, parse_mode="HTML")
    except Exception as exc:
        logger.exception("my_ads error")
        await notify_super_admins(message.bot, f"My ads xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Info
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "ℹ️ Ma'lumot")
async def show_info(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Shirin shahri Uy va Kvartira Bozori</b>\n\n"
        "Bu bot orqali siz:\n"
        "• 🏠 Uy-joy sotish/sotib olish e'lonlarini ko'rishingiz\n"
        "• 🏢 Kvartira ijaraga berish/olish e'lonlarini ko'rishingiz mumkin.\n\n"
        "❓ Savollar uchun adminga murojaat qiling.",
        parse_mode="HTML",
    )
