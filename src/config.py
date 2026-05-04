"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


@dataclass(frozen=True)
class Config:
    # Garmin Connect
    garmin_email: str
    garmin_password: str
    garmin_token_dir: Path

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Storage
    db_path: Path

    # Thresholds
    hr_high_bpm: int
    hr_resting_high_bpm: int
    alert_cooldown_seconds: int

    # Cadence
    poll_interval_minutes: int

    # Logging
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            garmin_email=_get("GARMIN_EMAIL"),
            garmin_password=_get("GARMIN_PASSWORD"),
            garmin_token_dir=Path(_get("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser(),
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
            db_path=Path(_get("DB_PATH", "./garmin.db")).expanduser(),
            hr_high_bpm=int(_get("HR_HIGH_BPM", "160")),
            hr_resting_high_bpm=int(_get("HR_RESTING_HIGH_BPM", "85")),
            alert_cooldown_seconds=int(_get("ALERT_COOLDOWN_SECONDS", "300")),
            poll_interval_minutes=int(_get("POLL_INTERVAL_MINUTES", "15")),
            log_level=_get("LOG_LEVEL", "INFO"),
        )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
