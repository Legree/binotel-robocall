"""
Telegram-інтерфейс: пишете боту «прозвони 54443» - він бере телефон агента
із заявки й ставить дзвінок у чергу. Результат прилітає тим самим чатом.
Працює тільки для чатів із TELEGRAM_ALLOWED_CHATS.
"""
import logging
import os
import re

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from travelon import BookingNotFound, get_booking

log = logging.getLogger("robocall.tg")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED = {c.strip() for c in os.getenv("TELEGRAM_ALLOWED_CHATS", "").split(",") if c.strip()}

bot = Bot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
dp = Dispatcher()

BOOKING_RE = re.compile(r"(?:прозвон\w*|подзвон\w*|call)\s*(?:заявк\w*)?\s*#?(\d{3,8})", re.I)


def allowed(m: Message) -> bool:
    return str(m.chat.id) in ALLOWED


async def enqueue_from_booking(booking_id: str, chat_id: int, urgent: bool):
    """Викликається з bot.py - щоб не було циклічного імпорту, передаємо функцію."""
    from bot import create_job  # noqa
    b = await get_booking(booking_id)
    job_id = create_job(b["agent_phone"], booking_id, urgent=urgent, tg_chat_id=chat_id)
    return b, job_id


@dp.message(Command("start", "help"))
async def help_cmd(m: Message):
    if not allowed(m):
        return await m.answer(f"Немає доступу. ID цього чату: {m.chat.id}")
    await m.answer("Напишіть: «прозвони заявку 54443» або /call 54443.\n"
                   "Додайте «не терміново», якщо без позначки терміновості.")


@dp.message(Command("call"))
@dp.message(F.text.regexp(BOOKING_RE))
async def call_cmd(m: Message):
    if not allowed(m):
        return
    match = BOOKING_RE.search(m.text) or re.search(r"(\d{3,8})", m.text)
    if not match:
        return await m.answer("Не бачу номер заявки.")
    booking_id = match.group(1)
    urgent = "не терміново" not in m.text.lower()
    try:
        b, job_id = await enqueue_from_booking(booking_id, m.chat.id, urgent)
    except BookingNotFound as e:
        return await m.answer(f"Заявку не знайдено або немає телефону: {e}")
    except Exception as e:
        log.exception("tg call failed")
        return await m.answer(f"Помилка: {e}")
    masked = b["agent_phone"][:-4] + "****"
    await m.answer(f"Дзвоню агенту {b['agent_name'] or ''} {masked} по заявці {booking_id}"
                   f"{' (терміново)' if urgent else ''}. Повідомлю результат.")


RESULT_TEXT = {
    "confirmed": "✅ Агент натиснув 1 і прослухав повідомлення",
    "nokey": "☎️ Трубку зняли, але 1 не натиснули",
    "machine": "🤖 Автовідповідач",
    "noanswer": "📵 Не взяв трубку",
    "busy": "📞 Зайнято",
    "hangup": "❌ Кинув трубку",
    "failed": "⚠️ Технічна помилка",
    "sms_sent": "📩 Не додзвонились, надіслано SMS з контактами",
    "sms_failed": "❗ Не додзвонились, SMS теж не пішло — зв'яжіться вручну",
}


async def notify_result(chat_id: int, booking_id: str, result: str, status: str, attempts: int):
    if not bot or not chat_id:
        return
    text = f"Заявка {booking_id}: {RESULT_TEXT.get(result, result)} (спроба {attempts})"
    if status == "pending":
        text += " — передзвоню пізніше"
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        log.warning("tg notify failed: %s", e)


async def run_polling():
    if not bot:
        log.info("TELEGRAM_TOKEN не задано - Telegram вимкнено")
        return
    await dp.start_polling(bot, handle_signals=False)
