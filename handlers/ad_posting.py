"""
FSM-based ad posting handler.
Only Seller and Owner roles can post ads.
Requires active subscription.
All user input is HTML-escaped.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from sqlalchemy import func, select

from database.connection import AsyncSessionFactory, get_session
from database.models import (
    Ad, AdStatus, AdType, Block, Setting, Subscription,
    SubscriptionType, User, UserRole,
)
from utils.notify import notify_relevant_admins, notify_super_admins

logger = logging.getLogger(__name__)
router = Router(name="ad_posting")



# ─────────────────────────────────────────────────────────────────────────────
# FSM
# ─────────────────────────────────────────────────────────────────────────────
class AdPostStates(StatesGroup):
    choose_block = State()
    enter_title = State()
    enter_description = State()
    enter_price = State()
    enter_rooms = State()
    enter_floor = State()
    enter_area = State()
    enter_phone = State()
    upload_media = State()
    confirm = State()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
SKIP_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="⏭️ O'tkazib yuborish")]],
    resize_keyboard=True, one_time_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
    resize_keyboard=True,
)


async def _get_active_sub(user_id: int) -> Subscription | None:
    now = datetime.now(timezone.utc)
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.sub_type.in_([SubscriptionType.standard, SubscriptionType.vip]),
                Subscription.is_active == True,  # noqa: E712
                Subscription.expires_at > now,
            )
        )
        return result.scalar_one_or_none()


async def _count_active_ads(user_id: int) -> int:
    async with AsyncSessionFactory() as session:
        from sqlalchemy import func
        result = await session.execute(
            select(func.count(Ad.id)).where(
                Ad.owner_id == user_id,
                Ad.status.in_([AdStatus.active, AdStatus.pending]),
            )
        )
        return result.scalar_one()


async def _get_setting(key: str) -> str:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        s = result.scalar_one_or_none()
        return s.value if s else "0"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📋 E'lon berish")
async def start_ad_post(message: Message, state: FSMContext) -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(select(User).where(User.id == message.from_user.id))
            user = result.scalar_one_or_none()

        if not user or user.role not in (UserRole.seller, UserRole.owner):
            await message.answer("❌ Bu funksiya faqat Sotuvchi va Kvartira egasi uchun.")
            return

        sub = await _get_active_sub(user.id)
        if not sub:
            await message.answer(
                "🔒 E'lon berish uchun avval obuna sotib olishingiz kerak.\n"
                "💳 <b>Obuna sotib olish</b> tugmasini bosing.",
                parse_mode="HTML",
            )
            return

        # Check ad limit
        limit_key = "vip_ads_limit" if sub.sub_type == SubscriptionType.vip else "standard_ads_limit"
        limit = int(await _get_setting(limit_key))
        current_count = await _count_active_ads(user.id)
        if current_count >= limit:
            await message.answer(
                f"⚠️ Siz allaqachon {current_count} ta faol e'lon bergansiz.\n"
                f"Obunangiz limiti: {limit} ta."
            )
            return

        # Fetch blocks
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Block).where(Block.is_active == True).order_by(Block.name)  # noqa: E712
            )
            blocks = result.scalars().all()

        if not blocks:
            await message.answer("Hozircha hech qanday blok mavjud emas. Admin tez orada qo'shadi.")
            return

        buttons = [
            [InlineKeyboardButton(text=f"🏘️ {b.name}", callback_data=f"adpost_block:{b.id}")]
            for b in blocks
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        await state.update_data(sub_type=sub.sub_type.value)
        await state.set_state(AdPostStates.choose_block)
        await message.answer(
            "📍 E'lon qaysi blokda joylashadi? Tanlang:",
            reply_markup=kb,
        )
    except Exception as exc:
        logger.exception("start_ad_post error")
        await notify_super_admins(message.bot, f"E'lon berish boshlanishi xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(AdPostStates.choose_block, F.data.startswith("adpost_block:"))
async def choose_block(call: CallbackQuery, state: FSMContext) -> None:
    block_id = int(call.data.split(":")[1])
    await state.update_data(block_id=block_id)
    await state.set_state(AdPostStates.enter_title)
    await call.message.answer(
        "📌 E'lon sarlavhasini kiriting (masalan: 3 xonali uy sotiladi):",
        reply_markup=CANCEL_KB,
    )
    await call.answer()


@router.message(AdPostStates.enter_title, F.text)
async def enter_title(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    title = html.escape(message.text.strip())
    if len(title) < 5 or len(title) > 200:
        await message.answer("⚠️ Sarlavha 5-200 ta belgidan iborat bo'lishi kerak.")
        return
    await state.update_data(title=title)
    await state.set_state(AdPostStates.enter_description)
    await message.answer("📝 E'lon tavsifini kiriting (manzil, holati, qo'shimcha ma'lumot):", reply_markup=CANCEL_KB)


@router.message(AdPostStates.enter_description, F.text)
async def enter_description(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    desc = html.escape(message.text.strip())
    if len(desc) < 10:
        await message.answer("⚠️ Tavsif kamida 10 ta belgidan iborat bo'lishi kerak.")
        return
    await state.update_data(description=desc)
    await state.set_state(AdPostStates.enter_price)
    await message.answer("💰 Narxni kiriting (faqat raqam, so'mda):", reply_markup=CANCEL_KB)


@router.message(AdPostStates.enter_price, F.text)
async def enter_price(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    price_str = message.text.strip().replace(" ", "").replace(",", "")
    if not price_str.isdigit():
        await message.answer("⚠️ Faqat raqam kiriting (masalan: 150000000).")
        return
    price = int(price_str)
    if price <= 0 or price > 10_000_000_000:
        await message.answer("⚠️ Narx noto'g'ri. Qaytadan kiriting.")
        return
    await state.update_data(price=price)
    await state.set_state(AdPostStates.enter_rooms)
    await message.answer("🚪 Xonalar sonini kiriting (masalan: 3):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_rooms, F.text)
async def enter_rooms(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if message.text == "⏭️ O'tkazib yuborish":
        await state.update_data(rooms=None)
    else:
        if not message.text.strip().isdigit():
            await message.answer("⚠️ Faqat raqam kiriting yoki o'tkazib yuboring.")
            return
        await state.update_data(rooms=int(message.text.strip()))
    await state.set_state(AdPostStates.enter_floor)
    await message.answer("🏢 Qavat raqamini kiriting (masalan: 3/5):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_floor, F.text)
async def enter_floor(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if message.text == "⏭️ O'tkazib yuborish":
        await state.update_data(floor=None, total_floors=None)
    else:
        parts = message.text.strip().split("/")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            await state.update_data(floor=int(parts[0]), total_floors=int(parts[1]))
        elif message.text.strip().isdigit():
            await state.update_data(floor=int(message.text.strip()), total_floors=None)
        else:
            await message.answer("⚠️ Format: 3/5 yoki faqat raqam kiriting.")
            return
    await state.set_state(AdPostStates.enter_area)
    await message.answer("📐 Maydonni kiriting (m², faqat raqam):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_area, F.text)
async def enter_area(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if message.text == "⏭️ O'tkazib yuborish":
        await state.update_data(area=None)
    else:
        if not message.text.strip().isdigit():
            await message.answer("⚠️ Faqat raqam kiriting.")
            return
        await state.update_data(area=int(message.text.strip()))
    await state.set_state(AdPostStates.enter_phone)
    await message.answer("📞 Telefon raqamingizni kiriting (masalan: +998901234567):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_phone, F.text)
async def enter_phone(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if message.text == "⏭️ O'tkazib yuborish":
        await state.update_data(contact_phone=None)
    else:
        phone = html.escape(message.text.strip())
        await state.update_data(contact_phone=phone)
    await state.set_state(AdPostStates.upload_media)
    media_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⏭️ O'tkazib yuborish")], [KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True,
    )
    await message.answer(
        "📸 Uy/kvartira rasmi yoki videosini yuboring (ixtiyoriy):",
        reply_markup=media_kb,
    )


@router.message(AdPostStates.upload_media, F.photo | F.video | F.text)
async def upload_media(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if message.text == "⏭️ O'tkazib yuborish":
        await state.update_data(media_file_id=None, media_type=None)
    elif message.photo:
        await state.update_data(media_file_id=message.photo[-1].file_id, media_type="photo")
    elif message.video:
        await state.update_data(media_file_id=message.video.file_id, media_type="video")
    else:
        await message.answer("⚠️ Iltimos, rasm, video yuboring yoki o'tkazib yuboring.")
        return
    await state.set_state(AdPostStates.confirm)
    await _show_ad_preview(message, state)


async def _show_ad_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = (
        "👀 <b>E'loningiz ko'rinishi:</b>\n\n"
        f"📌 Sarlavha: {data.get('title')}\n"
        f"📝 Tavsif: {data.get('description')}\n"
        f"💰 Narx: {data.get('price', 0):,} so'm\n"
    )
    if data.get("rooms"):
        text += f"🚪 Xonalar: {data['rooms']} ta\n"
    if data.get("floor"):
        floor_str = f"{data['floor']}/{data['total_floors']}" if data.get("total_floors") else str(data["floor"])
        text += f"🏢 Qavat: {floor_str}\n"
    if data.get("area"):
        text += f"📐 Maydon: {data['area']} m²\n"
    if data.get("contact_phone"):
        text += f"📞 Tel: {data['contact_phone']}\n"

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yuborish", callback_data="adpost_confirm"),
            InlineKeyboardButton(text="❌ Bekor", callback_data="adpost_cancel"),
        ]
    ])

    media_file_id = data.get("media_file_id")
    media_type = data.get("media_type")
    try:
        if media_file_id and media_type == "photo":
            await message.answer_photo(photo=media_file_id, caption=text, parse_mode="HTML", reply_markup=confirm_kb)
        elif media_file_id and media_type == "video":
            await message.answer_video(video=media_file_id, caption=text, parse_mode="HTML", reply_markup=confirm_kb)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=confirm_kb)
    except Exception:
        await message.answer(text, parse_mode="HTML", reply_markup=confirm_kb)


@router.callback_query(AdPostStates.confirm, F.data == "adpost_cancel")
async def adpost_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    await call.answer()


@router.callback_query(AdPostStates.confirm, F.data == "adpost_confirm")
async def adpost_confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    try:
        data = await state.get_data()
        await state.clear()

        async with AsyncSessionFactory() as session:
            result = await session.execute(select(User).where(User.id == call.from_user.id))
            user = result.scalar_one_or_none()

        if not user:
            await call.answer("Foydalanuvchi topilmadi.", show_alert=True)
            return

        ad_type = AdType.sale if user.role == UserRole.seller else AdType.rent
        sub_type_str = data.get("sub_type", "standard")
        sub_type = SubscriptionType(sub_type_str)

        # Calculate expiry from sub duration setting
        duration_key = "vip_duration_days" if sub_type == SubscriptionType.vip else "standard_duration_days"
        duration_days = int(await _get_setting(duration_key))
        expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

        async with get_session() as session:
            # ── Race-condition safe ad-limit check ────────────────────────
            # Lock the subscription row so parallel submits can't bypass limit
            sub_res = await session.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == call.from_user.id,
                    Subscription.sub_type.in_([SubscriptionType.standard, SubscriptionType.vip]),
                    Subscription.is_active == True,  # noqa: E712
                )
                .with_for_update()
            )
            locked_sub = sub_res.scalar_one_or_none()
            if not locked_sub:
                await call.answer("Obunangiz topilmadi yoki muddati tugagan.", show_alert=True)
                return

            limit_key = "vip_ads_limit" if locked_sub.sub_type == SubscriptionType.vip else "standard_ads_limit"
            limit = int(await _get_setting(limit_key))

            count_res = await session.execute(
                select(func.count(Ad.id)).where(
                    Ad.owner_id == call.from_user.id,
                    Ad.status.in_([AdStatus.active, AdStatus.pending]),
                )
            )
            current_count = count_res.scalar_one()
            if current_count >= limit:
                await call.answer(
                    f"Siz allaqachon {current_count}/{limit} ta e'lon bergansiz.",
                    show_alert=True,
                )
                return

            ad = Ad(
                owner_id=call.from_user.id,
                block_id=data["block_id"],
                ad_type=ad_type,
                sub_type=sub_type,
                title=data["title"],
                description=data["description"],
                price=data["price"],
                rooms=data.get("rooms"),
                floor=data.get("floor"),
                total_floors=data.get("total_floors"),
                area=data.get("area"),
                contact_phone=data.get("contact_phone"),
                media_file_id=data.get("media_file_id"),
                media_type=data.get("media_type"),
                status=AdStatus.pending,
                expires_at=expires_at,
            )
            session.add(ad)
            await session.flush()
            ad_id = ad.id

        await call.message.answer(
            "✅ E'loningiz adminga yuborildi! Tasdiqlanganidan so'ng e'lonlar taxtasida ko'rinadi.",
            reply_markup=ReplyKeyboardRemove(),
        )

        # Notify admins
        role_for_routing = "seller" if user.role == UserRole.seller else "owner"
        caption = (
            f"📋 <b>Yangi e'lon (#{ad_id})</b>\n\n"
            f"👤 {html.escape(user.full_name or '')} | ID: <code>{user.id}</code>\n"
            f"📦 Tur: {sub_type_str.upper()} | {'Sotish' if ad_type == AdType.sale else 'Ijara'}\n"
            f"📌 {data['title']}\n"
            f"💰 {data['price']:,} so'm"
        )
        approve_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ad_approve:{ad_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"ad_reject:{ad_id}"),
            ]
        ])

        media_file_id = data.get("media_file_id")
        media_type = data.get("media_type")
        await notify_relevant_admins(
            bot=bot,
            sub_type=None,
            user_role=role_for_routing,
            photo_file_id=media_file_id if media_type == "photo" else None,
            document_file_id=media_file_id if media_type == "video" else None,
            caption=caption,
            reply_markup=approve_kb,
            is_video=(media_type == "video"),
        )
        await call.answer()

    except Exception as exc:
        logger.exception("adpost_confirm error")
        await notify_super_admins(call.bot, f"E'lon tasdiqlash xatosi: {exc}")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)
