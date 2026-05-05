"""Telegram alerts with per-kind cooldown to avoid spam."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from . import db
from .config import Config

log = logging.getLogger(__name__)


def send_telegram(cfg: Config, text: str) -> bool:
    """Post a Markdown message to the configured chat. Returns True on 2xx."""
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": cfg.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


_send_telegram = send_telegram  # back-compat alias


def maybe_alert(
    cfg: Config,
    kind: str,
    text: str,
    payload: dict | None = None,
    *,
    cooldown_seconds: int | None = None,
) -> bool:
    """Send a Telegram alert if the cooldown for this kind has elapsed.

    Returns True if a message was sent, False if suppressed or failed.
    """
    cooldown = cooldown_seconds if cooldown_seconds is not None else cfg.alert_cooldown_seconds
    last = db.last_alert_ts(cfg.db_path, kind)
    if last is not None:
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        if elapsed < cooldown:
            log.debug("Alert '%s' suppressed (cooldown %.0fs remaining)",
                      kind, cooldown - elapsed)
            return False

    sent = send_telegram(cfg, text)
    db.log_alert(cfg.db_path, kind, json.dumps(payload or {}), sent)
    if sent:
        log.info("Alert sent: %s", kind)
    return sent


def test_alert(cfg: Config) -> bool:
    """Send a test message regardless of cooldown."""
    return send_telegram(cfg, "✅ *garmin-monitor* test alert — bot is working.")
