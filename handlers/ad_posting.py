"""
FSM-based ad posting handler — v3.

Media logic:
  - Foydalanuvchi rasmlarni yuboradi (bitta-bitta yoki album sifatida)
  - Telegram albumlarni media_group_id bilan yuboradi — biz ularni grupalaymiz
  - "✅ Tugadi" bosganda barcha bufferlangan media yig'iladi
  - Preview: barcha rasmlar media_group sifatida ko'rsatiladi
  - Video size guard: 500 MB limit
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

MAX_PHOTOS = 10
MAX_VIDEO_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB

# ─────────────────────────────────────────────────────────────────────────────
# FSM States
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
        [KeyboardButton(text="✅ Yuklash tugadi")],
        [KeyboardButton(text="⏭️ Media o'tkazib yuborish")],
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
    now = datetime.now(timezone.utc)
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.sub_type.in_([SubscriptionType.standard, SubscriptionType.vip]),
                Subscription.is_active == True,       # noqa: E712
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())
        )
        return result.scalars().first()


async def _get_effective_limit(user_id: int, sub: Subscription) -> int:
    limit_key = "vip_ads_limit" if sub.sub_type == SubscriptionType.vip else "standard_ads_limit"
    base = int(await _get_setting(limit_key))
    async with AsyncSessionFactory() as session:
        ov = (await session.execute(
            select(UserAdLimit).where(UserAdLimit.user_id == user_id)
        )).scalar_one_or_none()
    return base + (ov.extra_limit if ov else 0)


async def _count_active_ads(user_id: int) -> int:
    async with AsyncSessionFactory() as session:
        return (await session.execute(
            select(func.count(Ad.id)).where(
                Ad.owner_id == user_id,
                Ad.status.in_([AdStatus.active, AdStatus.pending]),
            )
        )).scalar_one()


def _is_cancel(text: str) -> bool:
    return text == "❌ Bekor qilish"


def _is_skip(text: str) -> bool:
    return text == "⏭️ O'tkazib yuborish"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
@router.message(F.text == "📋 E'lon berish")
async def start_ad_post(message: Message, state: FSMContext) -> None:
    try:
        async with AsyncSessionFactory() as session:
            user = (await session.execute(
                select(User).where(User.id == message.from_user.id)
            )).scalar_one_or_none()

        if not user or user.role not in (UserRole.seller, UserRole.owner):
            await message.answer("❌ Bu funksiya faqat Sotuvchi va Kvartira egasi uchun.")
            return

        sub = await _get_active_sub(user.id)
        if not sub:
            await message.answer(
                "🔒 E'lon berish uchun avval obuna sotib olishingiz kerak.\n"
                "💳 <b>Obuna sotib olish</b> tugmasini bosing.", parse_mode="HTML",
            )
            return

        limit = await _get_effective_limit(user.id, sub)
        count = await _count_active_ads(user.id)
        if count >= limit:
            await message.answer(
                f"⚠️ Limitingiz to'lgan: <b>{count}/{limit}</b> ta e'lon.\n"
                "Limit oshirish uchun adminga murojaat qiling.",
                parse_mode="HTML",
            )
            return

        async with AsyncSessionFactory() as session:
            blocks = (await session.execute(
                select(Block).where(Block.is_active == True).order_by(Block.name)  # noqa: E712
            )).scalars().all()

        if not blocks:
            await message.answer("Hozircha hech qanday blok mavjud emas.")
            return

        await state.update_data(sub_type=sub.sub_type.value)
        await state.set_state(AdPostStates.choose_block)
        await message.answer(
            "📍 E'lon qaysi blokda joylashadi?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🏘️ {b.name}", callback_data=f"adpost_block:{b.id}")]
                for b in blocks
            ]),
        )
    except Exception as exc:
        logger.exception("start_ad_post error")
        await notify_super_admins(message.bot, f"E'lon berish xatosi: {exc}")
        await message.answer("⚠️ Xatolik yuz berdi.")


@router.callback_query(AdPostStates.choose_block, F.data.startswith("adpost_block:"))
async def choose_block(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(block_id=int(call.data.split(":")[1]))
    await state.set_state(AdPostStates.enter_title)
    await call.message.answer("📌 E'lon sarlavhasini kiriting:", reply_markup=CANCEL_KB)
    await call.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Text input steps
# ─────────────────────────────────────────────────────────────────────────────
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
    if not raw.isdigit() or not (1 <= int(raw) <= 10_000_000_000):
        await message.answer("⚠️ Faqat raqam kiriting (masalan: 150000000).")
        return
    await state.update_data(price=int(raw))
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
    elif message.text.strip().isdigit():
        await state.update_data(rooms=int(message.text.strip()))
    else:
        await message.answer("⚠️ Faqat raqam kiriting yoki o'tkazib yuboring.")
        return
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
    elif message.text.strip().isdigit():
        await state.update_data(area=int(message.text.strip()))
    else:
        await message.answer("⚠️ Faqat raqam kiriting.")
        return
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
    # Clear any leftover media buffer from previous sessions
    await state.update_data(collected_photos=[], collected_video=None)
    await state.set_state(AdPostStates.upload_media)
    await message.answer(
        f"📸 <b>Rasm va video yuklash</b>\n\n"
        f"• Maksimum <b>{MAX_PHOTOS} ta rasm</b> yuboring\n"
        f"• Yoki <b>1 ta video</b> (max 500 MB)\n"
        f"• Rasmlarni bitta-bitta yoki album sifatida yuboring\n\n"
        f"Hammasini yuklab bo'lgach <b>✅ Yuklash tugadi</b> tugmasini bosing.",
        reply_markup=MEDIA_KB,
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Media upload — stores directly in FSM state (no separate buffer dict)
# ─────────────────────────────────────────────────────────────────────────────
@router.message(AdPostStates.upload_media, F.photo)
async def receive_photo(message: Message, state: FSMContext) -> None:
    """Each photo (including album photos) saved directly to FSM state."""
    data = await state.get_data()
    photos: list[dict] = data.get("collected_photos", [])

    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"⚠️ Maksimum {MAX_PHOTOS} ta rasm yuklash mumkin.")
        return

    p = message.photo[-1]  # largest size
    photos.append({
        "file_id": p.file_id,
        "file_unique_id": p.file_unique_id,
        "file_size": p.file_size or 0,
        "sort_order": len(photos),
    })
    await state.update_data(collected_photos=photos)

    # Show running counter (only for the first photo to avoid spam)
    if len(photos) == 1:
        await message.answer(
            f"✅ 1 ta rasm qabul qilindi.\n"
            f"Davom eting yoki <b>✅ Yuklash tugadi</b> ni bosing.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✅ {len(photos)} ta rasm qabul qilindi.",
        )


@router.message(AdPostStates.upload_media, F.video)
async def receive_video(message: Message, state: FSMContext) -> None:
    """Single video, 500 MB limit enforced."""
    v = message.video
    if v.file_size and v.file_size > MAX_VIDEO_SIZE_BYTES:
        size_mb = v.file_size / (1024 * 1024)
        await message.answer(
            f"⚠️ Video hajmi {size_mb:.0f} MB — maksimum 500 MB.\n"
            "Iltimos, kichikroq video yuboring."
        )
        return

    data = await state.get_data()
    if data.get("collected_video"):
        await message.answer("⚠️ Faqat 1 ta video yuklash mumkin. Avvalgi video o'chirildi.")

    await state.update_data(collected_video={
        "file_id": v.file_id,
        "file_unique_id": v.file_unique_id,
        "file_size": v.file_size or 0,
        "sort_order": 100,
    })
    await message.answer("✅ Video qabul qilindi. <b>✅ Yuklash tugadi</b> ni bosing.", parse_mode="HTML")


@router.message(AdPostStates.upload_media, F.text == "❌ Bekor qilish")
async def cancel_upload(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())


@router.message(AdPostStates.upload_media, F.text == "⏭️ Media o'tkazib yuborish")
async def skip_upload(message: Message, state: FSMContext) -> None:
    await state.update_data(collected_photos=[], collected_video=None)
    await state.set_state(AdPostStates.confirm)
    await _show_preview(message, state)


@router.message(AdPostStates.upload_media, F.text == "✅ Yuklash tugadi")
async def finish_upload(message: Message, state: FSMContext) -> None:
    """User signals end of upload. Show preview with all collected media."""
    data = await state.get_data()
    photos: list[dict] = data.get("collected_photos", [])
    video: Optional[dict] = data.get("collected_video")

    if not photos and not video:
        await message.answer(
            "📭 Hali hech qanday media yuklanmadi.\n"
            "Rasm/video yuboring yoki <b>⏭️ Media o'tkazib yuborish</b> ni bosing.",
            parse_mode="HTML",
        )
        return

    await state.set_state(AdPostStates.confirm)
    await _show_preview(message, state)


# ─────────────────────────────────────────────────────────────────────────────
# Preview — shows all uploaded media + ad info
# ─────────────────────────────────────────────────────────────────────────────
async def _show_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos: list[dict] = data.get("collected_photos", [])
    video: Optional[dict] = data.get("collected_video")

    # Build info text
    text = (
        "👀 <b>E'loningiz ko'rinishi:</b>\n\n"
        f"📌 <b>{data.get('title')}</b>\n"
        f"📝 {data.get('description')}\n"
        f"💰 Narx: <b>{data.get('price', 0):,} so'm</b>\n"
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

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yuborish", callback_data="adpost_confirm"),
            InlineKeyboardButton(text="❌ Bekor", callback_data="adpost_cancel"),
        ]
    ])

    # Send media preview first, then info+buttons
    try:
        if len(photos) > 1:
            # Album — send all photos as media group
            media_group = [
                InputMediaPhoto(media=photos[0]["file_id"], caption=f"📸 {len(photos)} ta rasm")
            ] + [InputMediaPhoto(media=p["file_id"]) for p in photos[1:]]
            await message.answer_media_group(media=media_group)
        elif len(photos) == 1:
            await message.answer_photo(
                photo=photos[0]["file_id"],
                caption="📸 1 ta rasm",
            )
        if video:
            await message.answer_video(
                video=video["file_id"],
                caption="🎬 Video",
            )
    except Exception as e:
        logger.warning("Preview media send error: %s", e)

    # Always send the text info + confirm buttons
    await message.answer(text, parse_mode="HTML", reply_markup=confirm_kb)


@router.callback_query(AdPostStates.confirm, F.data == "adpost_cancel")
async def adpost_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("❌ E'lon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    await call.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Final confirm — save to DB and notify admins
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(AdPostStates.confirm, F.data == "adpost_confirm")
async def adpost_confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    try:
        data = await state.get_data()
        await state.clear()

        async with AsyncSessionFactory() as session:
            user = (await session.execute(
                select(User).where(User.id == call.from_user.id)
            )).scalar_one_or_none()

        if not user:
            await call.answer("Foydalanuvchi topilmadi.", show_alert=True)
            return

        photos: list[dict] = data.get("collected_photos", [])
        video: Optional[dict] = data.get("collected_video")

        ad_type = AdType.sale if user.role == UserRole.seller else AdType.rent
        sub_type_str = data.get("sub_type", "standard")
        sub_type = SubscriptionType(sub_type_str)
        duration_key = "vip_duration_days" if sub_type == SubscriptionType.vip else "standard_duration_days"
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(await _get_setting(duration_key)))

        async with get_session() as session:
            # Race-condition safe subscription lock
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
            locked_sub = sub_res.scalars().first()
            if not locked_sub:
                await call.answer("Obunangiz topilmadi yoki muddati tugagan.", show_alert=True)
                return

            limit_key = "vip_ads_limit" if locked_sub.sub_type == SubscriptionType.vip else "standard_ads_limit"
            base_limit = int(await _get_setting(limit_key))
            # Check per-user override within the same session (no nested session)
            ov_res = await session.execute(
                select(UserAdLimit).where(UserAdLimit.user_id == call.from_user.id)
            )
            ov = ov_res.scalar_one_or_none()
            limit = base_limit + (ov.extra_limit if ov else 0)
            current_count = (await session.execute(
                select(func.count(Ad.id)).where(
                    Ad.owner_id == call.from_user.id,
                    Ad.status.in_([AdStatus.active, AdStatus.pending]),
                )
            )).scalar_one()
            if current_count >= limit:
                await call.answer(f"Limit to'lgan: {current_count}/{limit}.", show_alert=True)
                return

            # Create Ad
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
            await session.flush()
            ad_id = ad.id

            # Save media
            for m in photos:
                session.add(AdMedia(
                    ad_id=ad_id,
                    file_id=m["file_id"],
                    file_unique_id=m["file_unique_id"],
                    media_type=MediaType.photo,
                    file_size=m.get("file_size"),
                    sort_order=m.get("sort_order", 0),
                ))
            if video:
                session.add(AdMedia(
                    ad_id=ad_id,
                    file_id=video["file_id"],
                    file_unique_id=video["file_unique_id"],
                    media_type=MediaType.video,
                    file_size=video.get("file_size"),
                    sort_order=100,
                ))

        await call.message.answer(
            "✅ E'loningiz adminga yuborildi! Tasdiqlanganidan so'ng e'lonlar taxtasida ko'rinadi.",
            reply_markup=ReplyKeyboardRemove(),
        )

        # Build admin notification
        media_badge = ""
        if photos:
            media_badge += f" 📸{len(photos)}"
        if video:
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

        first_photo_id = photos[0]["file_id"] if photos else None
        first_video_id = video["file_id"] if video else None
        role_for_routing = "seller" if user.role == UserRole.seller else "owner"

        await notify_relevant_admins(
            bot=bot,
            sub_type=None,
            user_role=role_for_routing,
            photo_file_id=first_photo_id,
            document_file_id=first_video_id,
            caption=caption,
            reply_markup=approve_kb,
            is_video=bool(first_video_id and not first_photo_id),
        )
        await call.answer()

    except Exception as exc:
        logger.exception("adpost_confirm error")
        await notify_super_admins(call.bot, f"E'lon saqlash xatosi: {exc}")
        await call.answer("⚠️ Xatolik yuz berdi.", show_alert=True)
