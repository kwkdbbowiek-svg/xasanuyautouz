"""
Broadcast handler — send messages to all users.
Handles photo, video, audio, document, and plain text.
Uses try-except per user so one blocked bot doesn't halt the loop.
Only Super Admin can trigger.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from sqlalchemy import select

from database.connection import AsyncSessionFactory
from database.models import User
from filters.admin_filters import IsSuperAdmin
from utils.notify import notify_super_admins

logger = logging.getLogger(__name__)
router = Router(name="broadcast")

BATCH_SIZE = 25          # messages per batch
BATCH_DELAY = 0.05       # seconds between messages inside a batch
INTER_BATCH_DELAY = 1.0  # seconds between batches (Telegram rate limit)


class BroadcastStates(StatesGroup):
    waiting_message = State()
    confirm = State()


@router.message(F.text == "📢 Reklama yuborish", IsSuperAdmin())
async def broadcast_start(message: Message, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_message)
    cancel_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True,
    )
    await message.answer(
        "📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring.\n"
        "Rasm, video, audio, hujjat yoki matn bo'lishi mumkin.",
        reply_markup=cancel_kb,
    )


@router.message(
    BroadcastStates.waiting_message,
    IsSuperAdmin(),
    F.text | F.photo | F.video | F.audio | F.document | F.voice,
)
async def broadcast_receive(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("❌ Reklama bekor qilindi.", reply_markup=ReplyKeyboardRemove())
        return

    # Store message details in FSM
    msg_data: dict = {}
    if message.photo:
        msg_data = {"type": "photo", "file_id": message.photo[-1].file_id, "caption": message.caption or ""}
    elif message.video:
        msg_data = {"type": "video", "file_id": message.video.file_id, "caption": message.caption or ""}
    elif message.audio:
        msg_data = {"type": "audio", "file_id": message.audio.file_id, "caption": message.caption or ""}
    elif message.document:
        msg_data = {"type": "document", "file_id": message.document.file_id, "caption": message.caption or ""}
    elif message.voice:
        msg_data = {"type": "voice", "file_id": message.voice.file_id, "caption": ""}
    elif message.text:
        msg_data = {"type": "text", "text": html.escape(message.text)}
    else:
        await message.answer("⚠️ Bu media turi qo'llanilmaydi. Qaytadan urinib ko'ring.")
        return

    await state.update_data(broadcast_msg=msg_data)
    await state.set_state(BroadcastStates.confirm)

    # Count recipients
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
    count = len(users)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Yuborish ({count} kishi)", callback_data="broadcast_confirm"),
            InlineKeyboardButton(text="❌ Bekor", callback_data="broadcast_cancel"),
        ]
    ])
    await message.answer(
        f"📤 Xabar <b>{count}</b> ta foydalanuvchiga yuboriladi.\nTasdiqlaysizmi?",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(BroadcastStates.confirm, F.data == "broadcast_cancel", IsSuperAdmin())
async def broadcast_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("❌ Reklama bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    await call.answer()


@router.callback_query(BroadcastStates.confirm, F.data == "broadcast_confirm", IsSuperAdmin())
async def broadcast_confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    msg_data = data.get("broadcast_msg", {})
    await state.clear()

    await call.message.edit_reply_markup(reply_markup=None)
    status_msg = await call.message.answer("⏳ Reklama yuborilmoqda...", reply_markup=ReplyKeyboardRemove())
    await call.answer()

    async with AsyncSessionFactory() as session:
        result = await session.execute(select(User.id))
        user_ids = [row[0] for row in result.all()]

    success = 0
    failed = 0

    async def _safe_send(uid: int) -> bool:
        """Send to one user; return True on success."""
        try:
            await _send_broadcast_message(bot, uid, msg_data)
            return True
        except Exception as exc:
            err = str(exc).lower()
            # Silently ignore expected errors (user blocked bot / deactivated)
            if not any(k in err for k in ("bot was blocked", "user is deactivated",
                                           "chat not found", "forbidden")):
                logger.warning("Broadcast error → %s: %s", uid, exc)
            return False

    for i in range(0, len(user_ids), BATCH_SIZE):
        batch = user_ids[i: i + BATCH_SIZE]
        # Send the whole batch in parallel
        results = await asyncio.gather(*[_safe_send(uid) for uid in batch])
        success += sum(results)
        failed += len(results) - sum(results)
        # Respect Telegram's 30 msg/s global rate limit between batches
        await asyncio.sleep(INTER_BATCH_DELAY)

    try:
        await status_msg.edit_text(
            f"✅ Reklama yuborish yakunlandi!\n"
            f"✔️ Muvaffaqiyatli: {success}\n"
            f"❌ Yuborilmadi: {failed}"
        )
    except Exception:
        await call.message.answer(
            f"✅ Reklama yuborish yakunlandi!\n✔️ {success} / ❌ {failed}"
        )


async def _send_broadcast_message(bot: Bot, user_id: int, msg_data: dict) -> None:
    """Send one broadcast message to one user based on stored type."""
    msg_type = msg_data.get("type")
    caption = msg_data.get("caption", "")
    file_id = msg_data.get("file_id", "")

    if msg_type == "photo":
        await bot.send_photo(user_id, photo=file_id, caption=caption, parse_mode="HTML")
    elif msg_type == "video":
        await bot.send_video(user_id, video=file_id, caption=caption, parse_mode="HTML")
    elif msg_type == "audio":
        await bot.send_audio(user_id, audio=file_id, caption=caption, parse_mode="HTML")
    elif msg_type == "document":
        await bot.send_document(user_id, document=file_id, caption=caption, parse_mode="HTML")
    elif msg_type == "voice":
        await bot.send_voice(user_id, voice=file_id)
    elif msg_type == "text":
        await bot.send_message(user_id, text=msg_data.get("text", ""), parse_mode="HTML")
