"""
FSM-based ad posting handler — v2.

New features:
  - Media-group (album) collector: up to 10 photos + 1 video per ad
  - 500 MB video size guard
  - "Multiple rows" bug fix: .scalars().first() instead of scalar_one_or_none()
    on subscription queries that can return > 1 row
  - Race-condition safe ad-limit check with row-level lock
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from sqlalchemy import func, select

from database.connection import AsyncSessionFactory, get_session
from database.models import (
    Ad, AdMedia, AdStatus, AdType, Block, MediaType, Setting,
    Subscription, SubscriptionType, User, UserAdLimit, UserRole,
)
from utils.notify import notify_relevant_admins, notify_super_admins

logger = logging.getLogger(__name__)
router = Router(name="ad_posting")

# ── Constants ────────────────────────────────────────────────────────────────
MAX_PHOTOS = 10
MAX_VIDEO_SIZE_BYTES = 500 * 1024 * 1024   # 500 MB
# How long to wait for more media_group messages before treating as complete
MEDIA_GROUP_TIMEOUT = 2.0   # seconds

# ── In-memory media-group buffer: {user_id: [Message, ...]} ─────────────────
_media_group_buffer: dict[int, list[Message]] = {}
_media_group_tasks: dict[int, asyncio.Task] = {}


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
    upload_media = State()      # collecting photos / video
    confirm = State()


# ─────────────────────────────────────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────────────────────────────────────
SKIP_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="⏭️ O'tkazib yuborish")]],
    resize_keyboard=True, one_time_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
    resize_keyboard=True,
)
MEDIA_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Media yuklash tugadi")],
        [KeyboardButton(text="⏭️ O'tkazib yuborish")],
        [KeyboardButton(text="❌ Bekor qilish")],
    ],
    resize_keyboard=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _get_setting(key: str) -> str:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        s = result.scalar_one_or_none()
        return s.value if s else "0"


async def _get_active_sub(user_id: int) -> Optional[Subscription]:
    """
    FIX: Use .scalars().first() — a user can have multiple historical
    subscription rows; we want the most recent active one.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.sub_type.in_([SubscriptionType.standard, SubscriptionType.vip]),
                Subscription.is_active == True,          # noqa: E712
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())    # most future expiry first
        )
        return result.scalars().first()                  # ← NOT scalar_one_or_none()


async def _get_effective_limit(user_id: int, sub: Subscription) -> int:
    """Base limit from settings + any admin-granted extra limit."""
    limit_key = "vip_ads_limit" if sub.sub_type == SubscriptionType.vip else "standard_ads_limit"
    base = int(await _get_setting(limit_key))
    async with AsyncSessionFactory() as session:
        ov_res = await session.execute(
            select(UserAdLimit).where(UserAdLimit.user_id == user_id)
        )
        override = ov_res.scalar_one_or_none()
    extra = override.extra_limit if override else 0
    return base + extra


async def _count_active_ads(user_id: int) -> int:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(func.count(Ad.id)).where(
                Ad.owner_id == user_id,
                Ad.status.in_([AdStatus.active, AdStatus.pending]),
            )
        )
        return result.scalar_one()


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

        limit = await _get_effective_limit(user.id, sub)
        current_count = await _count_active_ads(user.id)
        if current_count >= limit:
            await message.answer(
                f"⚠️ Siz allaqachon <b>{current_count}</b> ta faol e'lon bergansiz.\n"
                f"Sizning limitingiz: <b>{limit}</b> ta.\n\n"
                "Limit ko'paytirish uchun adminga murojaat qiling.",
                parse_mode="HTML",
            )
            return

        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Block).where(Block.is_active == True).order_by(Block.name)  # noqa: E712
            )
            blocks = result.scalars().all()

        if not blocks:
            await message.answer("Hozircha hech qanday blok mavjud emas.")
            return

        buttons = [
            [InlineKeyboardButton(text=f"🏘️ {b.name}", callback_data=f"adpost_block:{b.id}")]
            for b in blocks
        ]
        await state.update_data(sub_type=sub.sub_type.value)
        await state.set_state(AdPostStates.choose_block)
        await message.answer(
            "📍 E'lon qaysi blokda joylashadi?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception as exc:
        logger.exception("start_ad_post error")
        await notify_super_admins(message.bot, f"E'lon berish xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Block
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(AdPostStates.choose_block, F.data.startswith("adpost_block:"))
async def choose_block(call: CallbackQuery, state: FSMContext) -> None:
    block_id = int(call.data.split(":")[1])
    await state.update_data(block_id=block_id)
    await state.set_state(AdPostStates.enter_title)
    await call.message.answer("📌 E'lon sarlavhasini kiriting:", reply_markup=CANCEL_KB)
    await call.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Steps 2-7 — Text fields
# ─────────────────────────────────────────────────────────────────────────────
def _is_cancel(text: str) -> bool:
    return text == "❌ Bekor qilish"


def _is_skip(text: str) -> bool:
    return text == "⏭️ O'tkazib yuborish"


@router.message(AdPostStates.enter_title, F.text)
async def enter_title(message: Message, state: FSMContext) -> None:
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    title = html.escape(message.text.strip())
    if not (5 <= len(title) <= 200):
        await message.answer("⚠️ Sarlavha 5–200 belgidan iborat bo'lishi kerak.")
        return
    await state.update_data(title=title)
    await state.set_state(AdPostStates.enter_description)
    await message.answer("📝 Tavsifini kiriting:", reply_markup=CANCEL_KB)


@router.message(AdPostStates.enter_description, F.text)
async def enter_description(message: Message, state: FSMContext) -> None:
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
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
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    raw = message.text.strip().replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await message.answer("⚠️ Faqat raqam kiriting (masalan: 150000000).")
        return
    price = int(raw)
    if not (1 <= price <= 10_000_000_000):
        await message.answer("⚠️ Narx noto'g'ri.")
        return
    await state.update_data(price=price)
    await state.set_state(AdPostStates.enter_rooms)
    await message.answer("🚪 Xonalar soni (masalan: 3):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_rooms, F.text)
async def enter_rooms(message: Message, state: FSMContext) -> None:
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if _is_skip(message.text):
        await state.update_data(rooms=None)
    else:
        if not message.text.strip().isdigit():
            await message.answer("⚠️ Faqat raqam kiriting yoki o'tkazib yuboring.")
            return
        await state.update_data(rooms=int(message.text.strip()))
    await state.set_state(AdPostStates.enter_floor)
    await message.answer("🏢 Qavat (masalan: 3/5):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_floor, F.text)
async def enter_floor(message: Message, state: FSMContext) -> None:
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if _is_skip(message.text):
        await state.update_data(floor=None, total_floors=None)
    else:
        parts = message.text.strip().split("/")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            await state.update_data(floor=int(parts[0]), total_floors=int(parts[1]))
        elif message.text.strip().isdigit():
            await state.update_data(floor=int(message.text.strip()), total_floors=None)
        else:
            await message.answer("⚠️ Format: 3/5 yoki faqat raqam.")
            return
    await state.set_state(AdPostStates.enter_area)
    await message.answer("📐 Maydon (m², raqam):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_area, F.text)
async def enter_area(message: Message, state: FSMContext) -> None:
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if _is_skip(message.text):
        await state.update_data(area=None)
    else:
        if not message.text.strip().isdigit():
            await message.answer("⚠️ Faqat raqam kiriting.")
            return
        await state.update_data(area=int(message.text.strip()))
    await state.set_state(AdPostStates.enter_phone)
    await message.answer("📞 Telefon (masalan: +998901234567):", reply_markup=SKIP_KB)


@router.message(AdPostStates.enter_phone, F.text)
async def enter_phone(message: Message, state: FSMContext) -> None:
    if _is_cancel(message.text):
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return
    if _is_skip(message.text):
        await state.update_data(contact_phone=None)
    else:
        await state.update_data(contact_phone=html.escape(message.text.strip()))
    await state.set_state(AdPostStates.upload_media)
    await message.answer(
        f"📸 Rasmlar yoki video yuboring.\n"
        f"• Maksimum <b>{MAX_PHOTOS} ta rasm</b> (album/media group)\n"
        f"• Yoki <b>1 ta video</b> (max 500 MB)\n"
        f"Tugatgach <b>✅ Media yuklash tugadi</b> tugmasini bosing.",
        reply_markup=MEDIA_KB,
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 8 — Media upload (album collector + video size guard)
# ─────────────────────────────────────────────────────────────────────────────
async def _flush_media_group(user_id: int, state: FSMContext, bot: Bot) -> None:
    """
    Called MEDIA_GROUP_TIMEOUT seconds after the first media arrives.
    Collects all buffered messages and stores media list in FSM state.
    """
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)

    messages = _media_group_buffer.pop(user_id, [])
    _media_group_tasks.pop(user_id, None)

    if not messages:
        return

    photos: list[dict] = []
    video: Optional[dict] = None

    for msg in messages:
        if msg.photo:
            if len(photos) < MAX_PHOTOS:
                p = msg.photo[-1]
                photos.append({
                    "file_id": p.file_id,
                    "file_unique_id": p.file_unique_id,
                    "file_size": p.file_size,
                })
        elif msg.video:
            v = msg.video
            if v.file_size and v.file_size > MAX_VIDEO_SIZE_BYTES:
                try:
                    await bot.send_message(
                        user_id,
                        f"⚠️ Video hajmi {v.file_size / (1024**2):.0f} MB — "
                        f"maksimum 500 MB ruxsat etiladi. Video qabul qilinmadi."
                    )
                except Exception:
                    pass
                continue
            if video is None:
                video = {
                    "file_id": v.file_id,
                    "file_unique_id": v.file_unique_id,
                    "file_size": v.file_size,
                }

    media_list = [{"media_type": "photo", **p, "sort_order": i} for i, p in enumerate(photos)]
    if video:
        media_list.append({
            "media_type": "video",
            **video,
            "sort_order": 100,
        })

    await state.update_data(media_list=media_list)

    count_text = ""
    if photos:
        count_text += f"📸 {len(photos)} ta rasm"
    if video:
        count_text += (" + " if photos else "") + "🎬 1 ta video"

    try:
        await bot.send_message(
            user_id,
            f"✅ Media qabul qilindi: {count_text}\n"
            "Endi e'lonni tasdiqlashingiz mumkin.",
        )
    except Exception:
        pass

    # Transition to confirm automatically after collecting
    await state.set_state(AdPostStates.confirm)
    # We need to send preview but don't have Message obj here — mark pending
    await state.update_data(preview_pending=True)


@router.message(AdPostStates.upload_media, F.photo | F.video)
async def receive_media(message: Message, state: FSMContext, bot: Bot) -> None:
    """Buffer incoming photos/videos and schedule flush after timeout."""
    user_id = message.from_user.id

    # Video size check immediately
    if message.video:
        v = message.video
        if v.file_size and v.file_size > MAX_VIDEO_SIZE_BYTES:
            await message.answer(
                f"⚠️ Video hajmi {v.file_size / (1024**2):.0f} MB — "
                f"maksimum 500 MB. Iltimos, kichikroq video yuboring."
            )
            return

    # Buffer the message
    if user_id not in _media_group_buffer:
        _media_group_buffer[user_id] = []
    _media_group_buffer[user_id].append(message)

    # Cancel existing flush task and restart timer (debounce)
    existing_task = _media_group_tasks.get(user_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    task = asyncio.create_task(_flush_media_group(user_id, state, bot))
    _media_group_tasks[user_id] = task


@router.message(AdPostStates.upload_media, F.text == "✅ Media yuklash tugadi")
async def media_upload_done(message: Message, state: FSMContext) -> None:
    """User manually signals end of media upload."""
    user_id = message.from_user.id

    # Cancel pending flush task — we flush now
    existing_task = _media_group_tasks.pop(user_id, None)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    messages = _media_group_buffer.pop(user_id, [])
    photos: list[dict] = []
    video: Optional[dict] = None

    for msg in messages:
        if msg.photo and len(photos) < MAX_PHOTOS:
            p = msg.photo[-1]
            photos.append({
                "file_id": p.file_id,
                "file_unique_id": p.file_unique_id,
                "file_size": p.file_size,
            })
        elif msg.video and video is None:
            v = msg.video
            if not (v.file_size and v.file_size > MAX_VIDEO_SIZE_BYTES):
                video = {
                    "file_id": v.file_id,
                    "file_unique_id": v.file_unique_id,
                    "file_size": v.file_size,
                }

    media_list = [{"media_type": "photo", **p, "sort_order": i} for i, p in enumerate(photos)]
    if video:
        media_list.append({"media_type": "video", **video, "sort_order": 100})

    await state.update_data(media_list=media_list)
    await state.set_state(AdPostStates.confirm)
    await _show_ad_preview(message, state)


@router.message(AdPostStates.upload_media, F.text == "⏭️ O'tkazib yuborish")
async def skip_media(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    t = _media_group_tasks.pop(user_id, None)
    if t and not t.done():
        t.cancel()
    _media_group_buffer.pop(user_id, None)
    await state.update_data(media_list=[])
    await state.set_state(AdPostStates.confirm)
    await _show_ad_preview(message, state)


@router.message(AdPostStates.upload_media, F.text == "❌ Bekor qilish")
async def cancel_media(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    t = _media_group_tasks.pop(user_id, None)
    if t and not t.done():
        t.cancel()
    _media_group_buffer.pop(user_id, None)
    await state.clear()
    await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())


# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — Preview & confirm
# ─────────────────────────────────────────────────────────────────────────────
async def _show_ad_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    media_list: list[dict] = data.get("media_list", [])
    photos = [m for m in media_list if m["media_type"] == "photo"]
    videos = [m for m in media_list if m["media_type"] == "video"]

    text = (
        "👀 <b>E'loningiz ko'rinishi:</b>\n\n"
        f"📌 Sarlavha: {data.get('title')}\n"
        f"📝 Tavsif: {data.get('description')}\n"
        f"💰 Narx: {data.get('price', 0):,} so'm\n"
    )
    if data.get("rooms"):
        text += f"🚪 Xonalar: {data['rooms']} ta\n"
    if data.get("floor"):
        fs = f"{data['floor']}/{data['total_floors']}" if data.get("total_floors") else str(data["floor"])
        text += f"🏢 Qavat: {fs}\n"
    if data.get("area"):
        text += f"📐 Maydon: {data['area']} m²\n"
    if data.get("contact_phone"):
        text += f"📞 Tel: {data['contact_phone']}\n"
    if photos:
        text += f"📸 Rasmlar: {len(photos)} ta\n"
    if videos:
        text += "🎬 Video: 1 ta\n"

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yuborish", callback_data="adpost_confirm"),
            InlineKeyboardButton(text="❌ Bekor", callback_data="adpost_cancel"),
        ]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=confirm_kb)


# Handle preview_pending (triggered from _flush_media_group auto-flush)
@router.message(AdPostStates.confirm, F.text)
async def confirm_state_text(message: Message, state: FSMContext) -> None:
    """Catch any stray text in confirm state — show preview."""
    data = await state.get_data()
    if data.get("preview_pending"):
        await state.update_data(preview_pending=False)
        await _show_ad_preview(message, state)


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

        duration_key = "vip_duration_days" if sub_type == SubscriptionType.vip else "standard_duration_days"
        duration_days = int(await _get_setting(duration_key))
        expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

        async with get_session() as session:
            # Race-condition safe: lock subscription row
            sub_res = await session.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == call.from_user.id,
                    Subscription.sub_type.in_([SubscriptionType.standard, SubscriptionType.vip]),
                    Subscription.is_active == True,   # noqa: E712
                    Subscription.expires_at > datetime.now(timezone.utc),
                )
                .order_by(Subscription.expires_at.desc())
                .with_for_update()
            )
            locked_sub = sub_res.scalars().first()   # ← .first() not scalar_one_or_none()
            if not locked_sub:
                await call.answer("Obunangiz topilmadi yoki muddati tugagan.", show_alert=True)
                return

            limit = await _get_effective_limit(call.from_user.id, locked_sub)
            count_res = await session.execute(
                select(func.count(Ad.id)).where(
                    Ad.owner_id == call.from_user.id,
                    Ad.status.in_([AdStatus.active, AdStatus.pending]),
                )
            )
            current_count = count_res.scalar_one()
            if current_count >= limit:
                await call.answer(
                    f"Limit to'lgan: {current_count}/{limit} ta e'lon.", show_alert=True
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
                status=AdStatus.pending,
                expires_at=expires_at,
            )
            session.add(ad)
            await session.flush()   # get ad.id
            ad_id = ad.id

            # Save media files
            media_list: list[dict] = data.get("media_list", [])
            for m in media_list:
                session.add(AdMedia(
                    ad_id=ad_id,
                    file_id=m["file_id"],
                    file_unique_id=m["file_unique_id"],
                    media_type=MediaType(m["media_type"]),
                    file_size=m.get("file_size"),
                    sort_order=m.get("sort_order", 0),
                ))

        await call.message.answer(
            "✅ E'loningiz adminga yuborildi! Tasdiqlanganidan so'ng e'lonlar taxtasida ko'rinadi.",
            reply_markup=ReplyKeyboardRemove(),
        )

        # Notify admins with preview
        role_for_routing = "seller" if user.role == UserRole.seller else "owner"
        media_list: list[dict] = data.get("media_list", [])
        photos = [m for m in media_list if m["media_type"] == "photo"]
        videos = [m for m in media_list if m["media_type"] == "video"]
        media_badge = ""
        if photos:
            media_badge += f" 📸{len(photos)}"
        if videos:
            media_badge += " 🎬1"

        caption = (
            f"📋 <b>Yangi e'lon #{ad_id}</b>{media_badge}\n\n"
            f"👤 {html.escape(user.full_name or '')} | <code>{user.id}</code>\n"
            f"📦 {sub_type_str.upper()} | {'Sotish' if ad_type == AdType.sale else 'Ijara'}\n"
            f"📌 {data['title']}\n"
            f"💰 {data['price']:,} so'm"
        )
        approve_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ad_approve:{ad_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"ad_reject:{ad_id}"),
            ]
        ])

        first_photo = photos[0]["file_id"] if photos else None
        first_video = videos[0]["file_id"] if videos else None
        await notify_relevant_admins(
            bot=bot,
            sub_type=None,
            user_role=role_for_routing,
            photo_file_id=first_photo,
            document_file_id=first_video,
            caption=caption,
            reply_markup=approve_kb,
            is_video=bool(first_video and not first_photo),
        )
        await call.answer()

    except Exception as exc:
        logger.exception("adpost_confirm error")
        await notify_super_admins(call.bot, f"E'lon saqlash xatosi: {exc}")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)
