"""Daily digest: a one-shot Telegram message summarizing yesterday's metrics.

Run from a systemd timer at e.g. 08:00 local time. Reads yesterday's row
from `daily_summary` plus the same-day HRV samples from `hrv` and posts a
short Markdown message via the Telegram bot.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import db
from .alerts import send_telegram
from .config import Config

log = logging.getLogger(__name__)


def _format_seconds(seconds: int | None) -> str:
    if not seconds:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def _ble_hrv_avg_for_date(db_path: Path, target_date: date) -> float | None:
    """Average RMSSD across BLE-derived HRV samples landing on target_date (local)."""
    start = datetime.combine(target_date, time(0, 0)).astimezone()
    end = start + timedelta(days=1)
    start_iso = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_iso = end.astimezone(timezone.utc).isoformat(timespec="seconds")

    with db.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT AVG(rmssd_ms) AS avg, COUNT(*) AS n FROM hrv "
            "WHERE ts >= ? AND ts < ?",
            (start_iso, end_iso),
        ).fetchone()
    if not row or not row["n"]:
        return None
    return float(row["avg"])


def build_digest(cfg: Config, target_date: date | None = None) -> str | None:
    """Build the digest text. Returns None if no data exists for the target date."""
    target_date = target_date or (date.today() - timedelta(days=1))
    summary = db.daily_summary_for(cfg.db_path, target_date.isoformat())
    ble_hrv = _ble_hrv_avg_for_date(cfg.db_path, target_date)

    if not summary and ble_hrv is None:
        return None

    s = summary or {}
    lines = [
        f"☀️ *Daily digest* — {target_date.isoformat()}",
        "",
        f"• Resting HR: *{s.get('resting_hr') or '—'}* bpm",
        f"• Max HR: {s.get('max_hr') or '—'} bpm",
        f"• Avg HR: {s.get('avg_hr') or '—'} bpm",
        f"• Steps: {s.get('steps') or '—'}",
        f"• Sleep: {_format_seconds(s.get('sleep_seconds'))}",
        f"• Stress avg: {s.get('stress_avg') or '—'}",
        f"• Body battery: {s.get('body_battery') or '—'}",
        f"• HRV (overnight): {s.get('hrv_overnight') or '—'} ms",
    ]
    if ble_hrv is not None:
        lines.append(f"• HRV (BLE day avg): {ble_hrv:.0f} ms")
    return "\n".join(lines)


def send_digest(cfg: Config, target_date: date | None = None) -> bool:
    text = build_digest(cfg, target_date)
    if text is None:
        log.info("No data for digest target date; skipping send")
        return False
    sent = send_telegram(cfg, text)
    log.info("Digest sent=%s for %s", sent, target_date or "yesterday")
    return sent
