"""Periodic Garmin Connect poller.

Designed to be run by a systemd timer (or cron) every ~15 minutes. Pulls
the daily summary, sleep, stress, and HRV payloads, normalizes them into
the `daily_summary` row, and triggers alerts on configured thresholds.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from garminconnect import Garmin

from . import alerts, db
from .config import Config
from .smart_alerts import run_smart_alerts

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


def normalize_stats(raw: dict | None) -> dict:
    """Pull HR / steps / body battery off the daily stats payload."""
    raw = raw or {}
    return {
        "resting_hr": _coerce_int(raw.get("restingHeartRate")),
        "max_hr": _coerce_int(raw.get("maxHeartRate")),
        "avg_hr": _coerce_int(
            raw.get("averageHeartRateInBeatsPerMinute")
            or raw.get("currentDayRestingHeartRate")
        ),
        "steps": _coerce_int(raw.get("totalSteps")),
        "body_battery": _coerce_int(raw.get("bodyBatteryMostRecentValue")),
    }


def normalize_sleep(raw: dict | None) -> dict:
    """Pull total sleep seconds out of the sleep payload."""
    raw = raw or {}
    dto = raw.get("dailySleepDTO") or {}
    return {
        "sleep_seconds": _coerce_int(
            dto.get("sleepTimeSeconds") or raw.get("sleepTimeSeconds")
        ),
    }


def normalize_stress(raw: dict | None) -> dict:
    """Pull average stress level out of the stress payload."""
    raw = raw or {}
    return {
        "stress_avg": _coerce_int(
            raw.get("avgStressLevel") or raw.get("averageStressLevel")
        ),
    }


def normalize_hrv(raw: dict | None) -> dict:
    """Pull overnight HRV average out of the HRV payload.

    The Connect API returns this as `hrvSummary.lastNightAvg` for users with
    HRV-status-capable devices. Older accounts may surface it on the daily
    stats payload directly (handled in normalize_stats fallback below).
    """
    raw = raw or {}
    summary = raw.get("hrvSummary") or {}
    return {
        "hrv_overnight": _coerce_int(
            summary.get("lastNightAvg") or raw.get("lastNightAvg")
        ),
    }


def fetch_daily(client: Garmin, cdate: str) -> dict:
    """Pull stats / sleep / stress / hrv and merge into a single flat dict.

    Each API call is wrapped so a single failure (e.g. HRV not enabled on the
    account) does not abort the whole poll.
    """
    fetched: dict[str, dict | None] = {
        "stats": None,
        "sleep": None,
        "stress": None,
        "hrv": None,
    }

    for key, fn in (
        ("stats", lambda: client.get_stats(cdate)),
        ("sleep", lambda: client.get_sleep_data(cdate)),
        ("stress", lambda: client.get_stress_data(cdate)),
        ("hrv", lambda: client.get_hrv_data(cdate)),
    ):
        try:
            fetched[key] = fn() or {}
        except Exception as e:  # noqa: BLE001 — Garmin client raises many types
            log.warning("Garmin %s fetch failed: %s", key, e)
            fetched[key] = None

    merged: dict = {}
    merged.update(normalize_stats(fetched["stats"]))
    merged.update(normalize_sleep(fetched["sleep"]))
    merged.update(normalize_stress(fetched["stress"]))
    merged.update(normalize_hrv(fetched["hrv"]))

    # Fallback: stats payload sometimes carries hrvLastNightAvg directly.
    if merged.get("hrv_overnight") is None and fetched["stats"]:
        merged["hrv_overnight"] = _coerce_int(
            fetched["stats"].get("hrvLastNightAvg")
        )

    merged["raw_json"] = json.dumps(fetched, default=str)
    return merged


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
    summary = fetch_daily(client, today)
    log.info(
        "Fetched: rhr=%s steps=%s sleep_s=%s stress=%s hrv=%s",
        summary.get("resting_hr"),
        summary.get("steps"),
        summary.get("sleep_seconds"),
        summary.get("stress_avg"),
        summary.get("hrv_overnight"),
    )
    db.upsert_daily_summary(cfg.db_path, today, summary)
    check_resting_hr(cfg, summary)
    run_smart_alerts(cfg)
    log.info("Poll complete")
