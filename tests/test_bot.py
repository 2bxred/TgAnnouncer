import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Message, Update

from bot import CREATE, LIST, Creation, build_dispatcher, main
from config import Config, load_config
from database import Database
from reminders import ReminderSender, display_time, parse_remind_at, validate_text

TOKEN = "123456789:" + "a" * 35
OWNER = 123456


class ValidationTests(unittest.TestCase):
    def test_text(self):
        self.assertEqual(validate_text("  hello  "), "hello")
        self.assertEqual(len(validate_text("x" * 1000)), 1000)
        for value in (None, "", " \n ", "x" * 1001):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                validate_text(value)

    def test_utc_and_display_round_trip(self):
        zone = ZoneInfo("Asia/Bangkok")
        result = parse_remind_at("02.01.2030 01:30", zone, datetime(2029, 1, 1, tzinfo=UTC))
        self.assertEqual(result, datetime(2030, 1, 1, 18, 30, tzinfo=UTC))
        self.assertEqual(display_time(result.isoformat(), zone), "02.01.2030 01:30")

    def test_invalid_and_past_dates(self):
        now = datetime(2030, 1, 1, tzinfo=UTC)
        for value in ("1.01.2031 01:00", "01.01.2031 1:00", "2031-01-01 01:00",
                      "31.02.2031 12:00", "01.01.2031 24:00", "01.01.2030 00:00",
                      "31.12.2029 23:59", "01.01.2031 12:00 ", "", "29.02.2031 12:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_remind_at(value, ZoneInfo("UTC"), now)
        parse_remind_at("29.02.2032 12:00", ZoneInfo("UTC"), now)

    def test_dst_gap_and_ambiguity(self):
        for value in ("31.03.2030 02:30", "27.10.2030 02:30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_remind_at(value, ZoneInfo("Europe/Berlin"), datetime(2029, 1, 1, tzinfo=UTC))

    def test_extreme_dates(self):
        for value, zone in (("01.01.0001 00:00", "Asia/Bangkok"),
                            ("31.12.9999 23:59", "America/New_York")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_remind_at(value, ZoneInfo(zone))

    def test_configuration(self):
        environment = {"BOT_TOKEN": TOKEN, "OWNER_ID": str(OWNER), "TIMEZONE": "Asia/Bangkok"}
        with patch("config.load_dotenv"), patch.dict("os.environ", environment, clear=True):
            self.assertEqual(load_config().owner_id, OWNER)
        for field, value in (("BOT_TOKEN", "bad"), ("OWNER_ID", "0"), ("OWNER_ID", "abc"),
                             ("TIMEZONE", "Not/AZone"), ("TIMEZONE", "")):
            with self.subTest(field=field, value=value), patch("config.load_dotenv"), patch.dict(
                "os.environ", {**environment, field: value}, clear=True
            ), self.assertRaises(ValueError):
                load_config()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "test.sqlite3")
        await self.database.open()
        self.bot = Bot(TOKEN)
        self.requests = []

        async def request(bot, method, timeout=None):
            self.requests.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            return Message.model_validate({
                "message_id": len(self.requests), "date": 1,
                "chat": {"id": OWNER, "type": "private"},
                "text": getattr(method, "text", ""),
            })

        self.bot.session.make_request = AsyncMock(side_effect=request)
        self.sender = ReminderSender(self.bot, self.database, OWNER)
        self.dp = build_dispatcher(Config(TOKEN, OWNER, ZoneInfo("Asia/Bangkok")), self.database, self.sender)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()
        await self.database.close()
        self.directory.cleanup()

    async def message(self, text=None, user_id=OWNER, private=True):
        self.update_id += 1
        message = {
            "message_id": self.update_id, "date": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user_id if private else -100, "type": "private" if private else "group"},
        }
        if text is not None:
            message["text"] = text
        if text and text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self.update_id, "message": message}))

    async def callback(self, data, user_id=OWNER):
        self.update_id += 1
        await self.dp.feed_update(self.bot, Update.model_validate({
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id), "chat_instance": "test", "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "message": {"message_id": 100, "date": 1, "chat": {"id": OWNER, "type": "private"}},
            },
        }))

    def state(self):
        return self.dp.fsm.get_context(bot=self.bot, chat_id=OWNER, user_id=OWNER)

    async def test_create_list_confirm_delete(self):
        await self.message("/start")
        self.assertEqual(self.requests[-1].reply_markup.keyboard[0][0].text, CREATE)
        await self.message(CREATE)
        await self.message("Купить молоко")
        await self.message("01.01.2099 12:30")
        rows = await self.database.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Купить молоко")
        self.assertEqual(datetime.fromisoformat(rows[0]["remind_at"]).hour, 5)
        self.assertIsNone(await self.state().get_state())
        await self.message(LIST)
        self.assertIn("Купить молоко", self.requests[-1].text)
        self.assertIn("01.01.2099 12:30", self.requests[-1].text)
        reminder_id = rows[0]["id"]
        await self.callback(f"delete:{reminder_id}")
        self.assertEqual(len(await self.database.pending()), 1)
        self.assertIn("Удалить напоминание?", self.requests[-1].text)
        await self.callback("keep")
        self.assertEqual(len(await self.database.pending()), 1)
        await self.callback(f"delete:{reminder_id}")
        await self.callback(f"confirm:{reminder_id}")
        self.assertEqual(await self.database.pending(), [])
        await self.callback(f"confirm:{reminder_id}")
        await self.message(LIST)
        self.assertIn("нет", self.requests[-1].text)

    async def test_validation_and_cancel(self):
        await self.message(CREATE)
        for text in (None, "  ", "x" * 1001):
            await self.message(text)
            self.assertEqual(await self.state().get_state(), Creation.text.state)
        await self.message("Текст")
        for text in (None, "wrong", "01.01.2000 00:00"):
            await self.message(text)
            self.assertEqual(await self.state().get_state(), Creation.time.state)
        await self.message("/cancel")
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(await self.database.pending(), [])
        for command in ("Отмена", "/start"):
            await self.message(CREATE)
            await self.message(command)
            self.assertIsNone(await self.state().get_state())

    async def test_access_control(self):
        reminder_id = await self.database.add("private", datetime.now(UTC) + timedelta(days=1))
        for text in ("/start", CREATE, LIST, "private", "01.01.2099 12:00"):
            await self.message(text, user_id=999)
        await self.message(LIST, private=False)
        self.assertEqual(self.requests, [])
        await self.callback(f"confirm:{reminder_id}", user_id=999)
        self.assertIsInstance(self.requests[-1], AnswerCallbackQuery)
        self.assertEqual(len(await self.database.pending()), 1)

    async def test_database_error_during_creation_is_recoverable(self):
        await self.message(CREATE)
        await self.message("Текст")
        with patch.object(self.database, "add", side_effect=aiosqlite.OperationalError("locked")):
            await self.message("01.01.2099 12:30")
        self.assertEqual(await self.state().get_state(), Creation.time.state)
        self.assertIn("Не удалось", self.requests[-1].text)
        await self.message("01.01.2099 12:30")
        self.assertEqual(len(await self.database.pending()), 1)

    async def test_telegram_handler_error_does_not_stop_dispatcher(self):
        self.bot.session.make_request.side_effect = TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="offline")
        await self.message("/start")
        self.bot.session.make_request.side_effect = None
        await self.message(CREATE)
        self.assertEqual(await self.state().get_state(), Creation.text.state)

    async def test_persistence_order_due_and_sent(self):
        now = datetime.now(UTC)
        later = await self.database.add("later", now + timedelta(days=1))
        earlier = await self.database.add("earlier", now - timedelta(minutes=1))
        await self.database.close()
        await self.database.open()
        self.assertEqual([r["id"] for r in await self.database.pending()], [earlier, later])
        await self.sender.check_once()
        self.assertEqual([r["id"] for r in await self.database.pending()], [later])
        self.assertIn("earlier", self.requests[-1].text)
        await self.sender.check_once()
        self.assertEqual(len(self.requests), 1)

    async def test_send_failure_is_delayed_then_retried(self):
        reminder_id = await self.database.add("retry", datetime.now(UTC) - timedelta(seconds=1))
        with patch.object(self.bot, "send_message", new_callable=AsyncMock) as send:
            send.side_effect = TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="offline")
            await self.sender.check_once()
            await self.sender.check_once()
            self.assertEqual(send.await_count, 1)
            self.assertIsNotNone(await self.database.get_pending(reminder_id))
            self.sender.retry_at[reminder_id] = 0
            send.side_effect = None
            await self.sender.check_once()
            self.assertEqual(send.await_count, 2)
            self.assertIsNone(await self.database.get_pending(reminder_id))

    async def test_rate_limit_pauses_all_reminders(self):
        for text in ("first", "second"):
            await self.database.add(text, datetime.now(UTC) - timedelta(seconds=1))
        with patch.object(self.bot, "send_message", new_callable=AsyncMock) as send:
            send.side_effect = TelegramRetryAfter(method=SendMessage(chat_id=OWNER, text="x"), message="flood", retry_after=120)
            await self.sender.check_once()
            await self.sender.check_once()
            self.assertEqual(send.await_count, 1)
            self.assertEqual(len(await self.database.pending()), 2)

    async def test_mark_sent_failure_retries_database_without_resending(self):
        reminder_id = await self.database.add("once", datetime.now(UTC) - timedelta(seconds=1))
        with patch.object(self.database, "mark_sent", side_effect=aiosqlite.OperationalError("locked")):
            await self.sender.check_once()
        self.assertEqual(len(self.requests), 1)
        self.sender.retry_at[reminder_id] = 0
        await self.sender.check_once()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(await self.database.pending(), [])

    async def test_background_loop_recovers_from_sqlite_error(self):
        with patch.object(self.sender, "check_once", new_callable=AsyncMock) as check, patch(
            "reminders.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            check.side_effect = [aiosqlite.OperationalError("locked"), None]
            sleep.side_effect = [None, asyncio.CancelledError]
            with self.assertRaises(asyncio.CancelledError):
                await self.sender.run()
            self.assertEqual(check.await_count, 2)
            self.assertEqual(sleep.await_args_list[0].args, (60,))

    async def test_startup_and_shutdown(self):
        database = Database(Path(self.directory.name) / "startup.sqlite3")
        sender = ReminderSender(self.bot, database, OWNER)
        config = Config(TOKEN, OWNER, ZoneInfo("UTC"))
        dp = build_dispatcher(config, database, sender)
        started = asyncio.Event()

        async def run_sender():
            await sender.check_once()
            started.set()
            await asyncio.Future()

        async def polling(*args, **kwargs):
            await asyncio.wait_for(started.wait(), timeout=2)

        with patch("bot.load_config", return_value=config), patch("bot.Database", return_value=database), patch(
            "bot.Bot", return_value=self.bot
        ), patch("bot.ReminderSender", return_value=sender), patch(
            "bot.build_dispatcher", return_value=dp
        ), patch.object(sender, "run", side_effect=run_sender), patch.object(
            dp, "start_polling", side_effect=polling
        ):
            await main()
        self.assertTrue(started.is_set())
        self.assertIsNone(database.connection)

    async def test_deleted_due_reminder_is_not_sent(self):
        reminder_id = await self.database.add("deleted", datetime.now(UTC) - timedelta(seconds=1))
        await self.callback(f"confirm:{reminder_id}")
        self.requests.clear()
        await self.sender.check_once()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
