"""Загрузка конфигурации из .env рядом с приложением."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    timezone: ZoneInfo


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    try:
        validate_token(token)
    except TokenValidationError:
        raise ValueError("Укажите корректный BOT_TOKEN в .env") from None
    try:
        owner_id = int(os.getenv("OWNER_ID", ""))
        if owner_id <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("OWNER_ID должен быть положительным целым числом") from None
    try:
        timezone = ZoneInfo(os.getenv("TIMEZONE", "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Укажите TIMEZONE, например Asia/Bangkok или Europe/Moscow") from None
    return Config(token, owner_id, timezone)
