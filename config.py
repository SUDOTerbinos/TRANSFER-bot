"""Environment-backed application configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{20,}$")


def _positive_int(name: str, value: str | None, default: int, maximum: int) -> int:
    raw = value if value is not None else str(default)
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= parsed <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return parsed


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: tuple[int, ...]
    database_path: Path
    default_action_quota: int = 50
    quota_window_hours: int = 24
    log_level: str = "INFO"
    scheduler_poll_seconds: float = 3.0
    invite_expiry_days: int = 7
    maximum_order_size: int = 100_000
    migration_announcement_template: str = "Community migration: {campaign_name}. Members are invited to request access voluntarily."

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError(
                "BOT_TOKEN is missing or malformed. Add a fresh BotFather token to .env."
            )

        raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
        admin_ids: list[int] = []
        for item in raw_admin_ids.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                user_id = int(item)
            except ValueError as exc:
                raise ValueError(
                    "ADMIN_IDS must be a comma-separated list of integers"
                ) from exc
            if user_id <= 0:
                raise ValueError("ADMIN_IDS values must be positive Telegram user IDs")
            if user_id not in admin_ids:
                admin_ids.append(user_id)
        database_path = Path(
            os.getenv("DATABASE_PATH", "data/telegram_migration.sqlite3").strip()
        ).expanduser()
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        announcement_template = os.getenv(
            "MIGRATION_ANNOUNCEMENT",
            "Community migration: {campaign_name}. Members are invited to request access voluntarily.",
        ).strip()
        if not 10 <= len(announcement_template) <= 500:
            raise ValueError(
                "MIGRATION_ANNOUNCEMENT must be between 10 and 500 characters"
            )

        return cls(
            bot_token=token,
            admin_ids=tuple(admin_ids),
            database_path=database_path,
            default_action_quota=_positive_int(
                "DEFAULT_ACTION_QUOTA",
                os.getenv("DEFAULT_ACTION_QUOTA"),
                50,
                100_000,
            ),
            quota_window_hours=_positive_int(
                "QUOTA_WINDOW_HOURS", os.getenv("QUOTA_WINDOW_HOURS"), 24, 24 * 365
            ),
            log_level=log_level,
            migration_announcement_template=announcement_template,
        )
