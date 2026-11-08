"""Небольшой слой SQLite. Все даты — ISO 8601 в UTC."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


def utc_string(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Ожидается дата с часовым поясом")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        # Не позволяет перемешать транзакции на одном соединении.
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.connection = await aiosqlite.connect(self.path, timeout=5)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 1000),
                remind_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sent')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reminders_status_time
                ON reminders(status, remind_at);
        """)
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def _write(self, sql: str, parameters: tuple) -> tuple[int, int]:
        async with self.lock:
            try:
                async with self.connection.execute(sql, parameters) as cursor:
                    result = (cursor.lastrowid, cursor.rowcount)
                await self.connection.commit()
                return result
            except aiosqlite.Error:
                await self.connection.rollback()
                raise

    async def add(self, text: str, remind_at: datetime) -> int:
        reminder_id, _ = await self._write(
            "INSERT INTO reminders(text, remind_at, created_at) VALUES (?, ?, ?)",
            (text, utc_string(remind_at), utc_string(datetime.now(UTC))),
        )
        return reminder_id

    async def pending(self, due_at: datetime | None = None) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM reminders WHERE status = 'pending'"
        parameters = ()
        if due_at is not None:
            sql += " AND remind_at <= ?"
            parameters = (utc_string(due_at),)
        sql += " ORDER BY remind_at, id"
        async with self.lock:
            async with self.connection.execute(sql, parameters) as cursor:
                return await cursor.fetchall()

    async def get_pending(self, reminder_id: int) -> aiosqlite.Row | None:
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM reminders WHERE id = ? AND status = 'pending'",
                (reminder_id,),
            ) as cursor:
                return await cursor.fetchone()

    async def mark_sent(self, reminder_id: int) -> None:
        await self._write(
            "UPDATE reminders SET status = 'sent' WHERE id = ?", (reminder_id,)
        )

    async def delete(self, reminder_id: int) -> bool:
        _, count = await self._write(
            "DELETE FROM reminders WHERE id = ? AND status = 'pending'", (reminder_id,)
        )
        return count > 0
