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
    hr_resting_high_bpm: int
    alert_cooldown_seconds: int

    # Cadence
    poll_interval_minutes: int

    # Logging
    log_level: str

    # Biometrics (for calorie calculations)
    user_age: int
    user_height_cm: int
    user_weight_kg: int
    user_sex: str  # 'male' or 'female'

    # Nutrition / training targets
    protein_target_g_per_kg: float
    kcal_target: int
    sleep_target_hours: float
    user_hrmax: int  # 0 → fall back to 220 - age

    # Claude (for the Telegram meal-logging bot). Set ONE of these — the
    # OAuth token route bills calls to the user's Claude Code subscription;
    # the API key route bills the workspace's pay-as-you-go credits.
    anthropic_api_key: str
    claude_oauth_token: str
    claude_model: str

    @classmethod
    def from_env(cls) -> "Config":
        age = int(_get("USER_AGE", "30"))
        return cls(
            garmin_email=_get("GARMIN_EMAIL"),
            garmin_password=_get("GARMIN_PASSWORD"),
            garmin_token_dir=Path(_get("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser(),
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
            db_path=Path(_get("DB_PATH", "./garmin.db")).expanduser(),
            hr_resting_high_bpm=int(_get("HR_RESTING_HIGH_BPM", "85")),
            alert_cooldown_seconds=int(_get("ALERT_COOLDOWN_SECONDS", "300")),
            poll_interval_minutes=int(_get("POLL_INTERVAL_MINUTES", "15")),
            log_level=_get("LOG_LEVEL", "INFO"),
            user_age=age,
            user_height_cm=int(_get("USER_HEIGHT_CM", "175")),
            user_weight_kg=int(_get("USER_WEIGHT_KG", "75")),
            user_sex=_get("USER_SEX", "male"),
            protein_target_g_per_kg=float(_get("PROTEIN_TARGET_G_PER_KG", "1.6")),
            kcal_target=int(_get("KCAL_TARGET", "2200")),
            sleep_target_hours=float(_get("SLEEP_TARGET_HOURS", "8")),
            user_hrmax=int(_get("USER_HRMAX", "0")),
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            claude_oauth_token=_get("CLAUDE_CODE_OAUTH_TOKEN"),
            claude_model=_get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
