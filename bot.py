"""Запуск: python bot.py."""

import asyncio
import logging
from contextlib import suppress

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

from config import BASE_DIR, Config, load_config
from database import Database
from reminders import ReminderSender, display_time, parse_remind_at, validate_text

logger = logging.getLogger(__name__)
CREATE = "Создать напоминание"
LIST = "Мои напоминания"
CANCEL = "Отмена"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CREATE)], [KeyboardButton(text=LIST)]],
    resize_keyboard=True,
)
CANCEL_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CANCEL)]], resize_keyboard=True,
)


class Creation(StatesGroup):
    text = State()
    time = State()


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = event.from_user
        chat = event.chat if isinstance(event, Message) else getattr(event.message, "chat", None)
        if user is None or user.id != self.owner_id or chat is None or chat.type != "private":
            if isinstance(event, CallbackQuery):
                with suppress(TelegramAPIError, OSError):
                    await event.answer("Доступ запрещён", show_alert=True)
            return None
        return await handler(event, data)


def delete_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Удалить", callback_data=f"delete:{reminder_id}")
    ]])


def build_dispatcher(config: Config, database: Database, sender: ReminderSender) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    router = Router()
    router.message.outer_middleware(OwnerOnly(config.owner_id))
    router.callback_query.outer_middleware(OwnerOnly(config.owner_id))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Выберите действие:", reply_markup=MENU)

    @router.message(Command("cancel"))
    @router.message(F.text == CANCEL)
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Создание отменено.", reply_markup=MENU)

    @router.message(F.text == CREATE)
    async def create(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Creation.text)
        await message.answer("Введите текст напоминания (до 1000 символов):", reply_markup=CANCEL_MENU)

    @router.message(F.text == LIST)
    async def list_reminders(message: Message, state: FSMContext):
        await state.clear()
        rows = await database.pending()
        await message.answer("Активные напоминания:" if rows else "Активных напоминаний нет.", reply_markup=MENU)
        for row in rows:
            await message.answer(
                f"{row['text']}\n\n{display_time(row['remind_at'], config.timezone)} ({config.timezone.key})",
                reply_markup=delete_keyboard(row["id"]),
            )

    @router.message(Creation.text)
    async def receive_text(message: Message, state: FSMContext):
        try:
            text = validate_text(message.text)
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(text=text)
        await state.set_state(Creation.time)
        await message.answer(
            f"Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.\nЧасовой пояс: {config.timezone.key}",
            reply_markup=CANCEL_MENU,
        )

    @router.message(Creation.time)
    async def receive_time(message: Message, state: FSMContext):
        try:
            remind_at = parse_remind_at(message.text or "", config.timezone)
        except ValueError as error:
            await message.answer(str(error))
            return
        data = await state.get_data()
        await database.add(data["text"], remind_at)
        await state.clear()
        await message.answer(
            f"Напоминание создано: {display_time(remind_at, config.timezone)} ({config.timezone.key}).",
            reply_markup=MENU,
        )

    @router.callback_query(F.data.regexp(r"^delete:[0-9]+$"))
    async def ask_delete(callback: CallbackQuery):
        reminder_id = int(callback.data.split(":")[1])
        row = await database.get_pending(reminder_id)
        if row is None:
            await callback.answer("Напоминание уже удалено или отправлено.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Удалить напоминание?\n\n{row['text']}\n\n{display_time(row['remind_at'], config.timezone)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, удалить", callback_data=f"confirm:{reminder_id}"),
                InlineKeyboardButton(text="Отмена", callback_data="keep"),
            ]]),
        )

    @router.callback_query(F.data.regexp(r"^confirm:[0-9]+$"))
    async def confirm_delete(callback: CallbackQuery):
        reminder_id = int(callback.data.split(":")[1])
        async with sender.delivery_lock:
            deleted = await database.delete(reminder_id)
        await callback.answer()
        await callback.message.edit_text(
            "Напоминание удалено." if deleted else "Напоминание уже удалено или отправлено."
        )

    @router.callback_query(F.data == "keep")
    async def keep(callback: CallbackQuery):
        await callback.answer()
        await callback.message.edit_text("Удаление отменено.")

    @router.message()
    async def fallback(message: Message):
        await message.answer("Выберите действие в меню.", reply_markup=MENU)

    @router.errors()
    async def handle_error(event: ErrorEvent):
        if not isinstance(event.exception, (aiosqlite.Error, TelegramAPIError, OSError)):
            return False
        # Не выводим токен, текст напоминания или полный запрос Telegram в лог.
        logger.error("Ошибка обработки запроса: %s", type(event.exception).__name__)
        message = event.update.message
        callback = event.update.callback_query
        with suppress(TelegramAPIError, OSError):
            if callback:
                await callback.answer("Не удалось выполнить действие. Попробуйте позже.", show_alert=True)
            elif message:
                await message.answer("Не удалось выполнить действие. Попробуйте ещё раз через минуту.")
        return True

    dp.include_router(router)
    return dp


async def main() -> None:
    config = load_config()
    database = Database(BASE_DIR / "reminders.sqlite3")
    bot = Bot(config.bot_token)
    task = None
    dp = None
    try:
        await database.open()
        sender = ReminderSender(bot, database, config.owner_id)
        dp = build_dispatcher(config, database, sender)
        # Убираем webhook, сохраняя накопившиеся обновления.
        await bot.delete_webhook(drop_pending_updates=False)
        task = asyncio.create_task(sender.run(), name="reminder-sender")
        await dp.start_polling(
            bot, allowed_updates=["message", "callback_query"],
            handle_as_tasks=False, close_bot_session=False,
        )
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if dp is not None:
            await dp.storage.close()
        await database.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except (ValueError, aiosqlite.Error, TelegramAPIError, OSError) as error:
        logger.error("Не удалось запустить бота (%s). Проверьте .env, сеть и доступ к SQLite.", type(error).__name__)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
