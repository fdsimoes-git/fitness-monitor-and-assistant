"""Periodic Garmin Connect poller.

Designed to be run by a systemd timer (or cron) every ~15 minutes. Pulls
today's daily summary, upserts into SQLite, and triggers alerts on
configured thresholds.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from garminconnect import Garmin

from . import alerts, db
from .config import Config

log = logging.getLogger(__name__)


def _login(cfg: Config) -> Garmin:
    client = Garmin(cfg.garmin_email, cfg.garmin_password)
    # The library auto-creates and refreshes tokens in the token dir
    client.login(str(cfg.garmin_token_dir))
    return client


def _coerce_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch_daily_summary(client: Garmin, cdate: str) -> dict:
    """Pull today's stats and normalize into a flat dict matching db.daily_summary."""
    raw = client.get_stats(cdate) or {}
    # Field names below mirror what python-garminconnect typically returns;
    # confirm against your account's payload and adjust as needed.
    return {
        "resting_hr": _coerce_int(raw.get("restingHeartRate")),
        "max_hr": _coerce_int(raw.get("maxHeartRate")),
        "avg_hr": _coerce_int(raw.get("averageHeartRateInBeatsPerMinute")
                              or raw.get("currentDayRestingHeartRate")),
        "steps": _coerce_int(raw.get("totalSteps")),
        "sleep_seconds": _coerce_int(raw.get("sleepingSeconds")),
        "stress_avg": _coerce_int(raw.get("averageStressLevel")),
        "body_battery": _coerce_int(raw.get("bodyBatteryMostRecentValue")),
        "hrv_overnight": _coerce_int(raw.get("hrvLastNightAvg")),
        "raw_json": json.dumps(raw),
    }


def check_resting_hr(cfg: Config, summary: dict) -> None:
    rhr = summary.get("resting_hr")
    if rhr and rhr >= cfg.hr_resting_high_bpm:
        alerts.maybe_alert(
            cfg,
            kind="resting_hr_high",
            text=(
                f"📈 Resting HR elevated: *{rhr}* bpm "
                f"(threshold {cfg.hr_resting_high_bpm})"
            ),
            payload={"resting_hr": rhr},
        )


def run_once(cfg: Config) -> None:
    today = date.today().isoformat()
    log.info("Polling Garmin Connect for %s", today)
    client = _login(cfg)
    summary = fetch_daily_summary(client, today)
    log.info("Fetched: rhr=%s steps=%s sleep_s=%s stress=%s",
             summary.get("resting_hr"),
             summary.get("steps"),
             summary.get("sleep_seconds"),
             summary.get("stress_avg"))
    db.upsert_daily_summary(cfg.db_path, today, summary)
    check_resting_hr(cfg, summary)
    log.info("Poll complete")
