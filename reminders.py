"""Валидация времени и простой фоновый цикл отправки."""

import asyncio
import logging
import re
from datetime import UTC, datetime
from time import monotonic
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database import Database

logger = logging.getLogger(__name__)
DATE_FORMAT = "%d.%m.%Y %H:%M"
CHECK_INTERVAL = 5
RETRY_DELAY = 60


def validate_text(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("Введите непустой текст напоминания.")
    if len(text) > 1000:
        raise ValueError("Текст должен содержать не более 1000 символов.")
    return text


def parse_remind_at(text: str, timezone: ZoneInfo, now: datetime | None = None) -> datetime:
    if not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4} [0-9]{2}:[0-9]{2}", text):
        raise ValueError("Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.")
    try:
        local = datetime.strptime(text, DATE_FORMAT)
    except ValueError:
        raise ValueError("Такой даты или времени не существует.") from None
    # Round-trip обнаруживает пропущенный час; fold — повторённый час при DST.
    candidates = set()
    for fold in (0, 1):
        try:
            utc = local.replace(tzinfo=timezone, fold=fold).astimezone(UTC)
            if utc.astimezone(timezone).replace(tzinfo=None) == local:
                candidates.add(utc)
        except OverflowError:
            raise ValueError("Дата выходит за поддерживаемый диапазон. Выберите другую.") from None
    if len(candidates) != 1:
        raise ValueError("Время не существует или неоднозначно из-за перевода часов. Выберите другое.")
    result = candidates.pop()
    if result <= (now or datetime.now(UTC)):
        raise ValueError("Напоминание должно быть в будущем.")
    return result


def display_time(value: str | datetime, timezone: ZoneInfo) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(timezone).strftime(DATE_FORMAT)


class ReminderSender:
    def __init__(self, bot: Bot, database: Database, owner_id: int):
        self.bot = bot
        self.database = database
        self.owner_id = owner_id
        self.retry_at: dict[int, float] = {}
        self.blocked_until = 0.0
        # Если Telegram уже принял сообщение, при ошибке SQLite повторяем только запись.
        self.delivered: set[int] = set()
        # Сериализует удаление и отправку, исключая отправку после успешного удаления.
        self.delivery_lock = asyncio.Lock()

    async def check_once(self) -> None:
        if monotonic() < self.blocked_until:
            return
        rows = await self.database.pending(datetime.now(UTC))
        active_ids = {row["id"] for row in rows}
        self.retry_at = {key: value for key, value in self.retry_at.items() if key in active_ids}
        self.delivered.intersection_update(active_ids)
        for row in rows:
            reminder_id = row["id"]
            if self.retry_at.get(reminder_id, 0) > monotonic():
                continue
            async with self.delivery_lock:
                if await self.database.get_pending(reminder_id) is None:
                    continue
                try:
                    if reminder_id not in self.delivered:
                        await self.bot.send_message(self.owner_id, f"⏰ Напоминание\n\n{row['text']}")
                        self.delivered.add(reminder_id)
                    await self.database.mark_sent(reminder_id)
                    self.delivered.discard(reminder_id)
                    self.retry_at.pop(reminder_id, None)
                except TelegramRetryAfter as error:
                    self.blocked_until = monotonic() + max(RETRY_DELAY, error.retry_after)
                    self.retry_at[reminder_id] = self.blocked_until
                    logger.warning("Telegram ограничил частоту отправки; повтор отложен")
                    break
                except (TelegramAPIError, OSError, aiosqlite.Error) as error:
                    self.retry_at[reminder_id] = monotonic() + RETRY_DELAY
                    logger.warning("Не удалось обработать напоминание %s: %s", reminder_id, type(error).__name__)

    async def run(self) -> None:
        while True:
            try:
                await self.check_once()
            except (aiosqlite.Error, OSError):
                logger.exception("Ошибка проверки напоминаний; следующая попытка через минуту")
                await asyncio.sleep(RETRY_DELAY)
            else:
                await asyncio.sleep(CHECK_INTERVAL)
