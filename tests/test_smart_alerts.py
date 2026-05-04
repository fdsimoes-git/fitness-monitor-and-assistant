"""Tests for the higher-level alert checks (trend, HRV drop, night-HR)."""
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src import db, smart_alerts
from src.config import Config


def _make_cfg(db_path: Path) -> Config:
    return Config(
        garmin_email="x",
        garmin_password="x",
        garmin_token_dir=Path("/tmp/x"),
        telegram_bot_token="x",
        telegram_chat_id="x",
        db_path=db_path,
        hr_high_bpm=160,
        hr_resting_high_bpm=85,
        alert_cooldown_seconds=0,  # disabled for tests
        poll_interval_minutes=15,
        log_level="INFO",
    )


@pytest.fixture
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        yield p


def _set_summary(p: Path, date_str: str, **fields) -> None:
    db.upsert_daily_summary(p, date_str, fields)


def test_trend_alert_triggers_on_5_day_climb(tmpdb):
    cfg = _make_cfg(tmpdb)
    for i, rhr in enumerate([60, 62, 63, 65, 67]):
        _set_summary(tmpdb, f"2026-04-{20 + i:02d}", resting_hr=rhr)

    with patch("src.smart_alerts.alerts.maybe_alert", return_value=True) as mock:
        sent = smart_alerts.check_resting_hr_trend(cfg)
    assert sent is True
    assert mock.call_args.kwargs["kind"] == "resting_hr_trend"


def test_trend_alert_skips_when_not_strictly_increasing(tmpdb):
    cfg = _make_cfg(tmpdb)
    for i, rhr in enumerate([60, 62, 62, 65, 67]):  # repeat -> not strict
        _set_summary(tmpdb, f"2026-04-{20 + i:02d}", resting_hr=rhr)
    with patch("src.smart_alerts.alerts.maybe_alert") as mock:
        sent = smart_alerts.check_resting_hr_trend(cfg)
    assert sent is False
    assert not mock.called


def test_trend_alert_skips_with_too_few_days(tmpdb):
    cfg = _make_cfg(tmpdb)
    for i, rhr in enumerate([60, 62, 64]):
        _set_summary(tmpdb, f"2026-04-{20 + i:02d}", resting_hr=rhr)
    with patch("src.smart_alerts.alerts.maybe_alert") as mock:
        assert smart_alerts.check_resting_hr_trend(cfg) is False
    assert not mock.called


def test_hrv_drop_triggers_when_below_baseline(tmpdb):
    cfg = _make_cfg(tmpdb)
    # 7 baseline days at 50 ms, then last night at 35 ms -> 30% drop
    for i, hrv in enumerate([50, 52, 48, 51, 50, 49, 50, 35]):
        _set_summary(tmpdb, f"2026-04-{15 + i:02d}", hrv_overnight=hrv)
    with patch("src.smart_alerts.alerts.maybe_alert", return_value=True) as mock:
        sent = smart_alerts.check_hrv_drop(cfg)
    assert sent is True
    assert mock.call_args.kwargs["kind"] == "hrv_drop"


def test_hrv_drop_skips_when_within_threshold(tmpdb):
    cfg = _make_cfg(tmpdb)
    # Baseline ~50, last 45 -> 10% drop, below 20% threshold
    for i, hrv in enumerate([50, 52, 48, 51, 50, 49, 50, 45]):
        _set_summary(tmpdb, f"2026-04-{15 + i:02d}", hrv_overnight=hrv)
    with patch("src.smart_alerts.alerts.maybe_alert") as mock:
        assert smart_alerts.check_hrv_drop(cfg) is False
    assert not mock.called


def test_hrv_drop_skips_with_insufficient_history(tmpdb):
    cfg = _make_cfg(tmpdb)
    for i, hrv in enumerate([50, 30]):  # only 2 samples total
        _set_summary(tmpdb, f"2026-04-{20 + i:02d}", hrv_overnight=hrv)
    with patch("src.smart_alerts.alerts.maybe_alert") as mock:
        assert smart_alerts.check_hrv_drop(cfg) is False
    assert not mock.called


def test_hr_resting_alert_triggers_on_high_night_avg(tmpdb):
    cfg = _make_cfg(tmpdb)
    # Insert HR samples in last night's rest window.
    now_local = datetime.now().astimezone()
    start_utc, end_utc = smart_alerts._last_rest_window_utc(now_local)
    midpoint = start_utc + (end_utc - start_utc) / 2

    with sqlite3.connect(tmpdb) as conn:
        for i in range(15):
            ts = (midpoint + timedelta(minutes=i)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO hr_realtime (ts, bpm) VALUES (?, ?)", (ts, 95)
            )

    with patch("src.smart_alerts.alerts.maybe_alert", return_value=True) as mock:
        sent = smart_alerts.check_hr_while_resting(cfg, now=now_local)
    assert sent is True
    assert mock.call_args.kwargs["kind"] == "hr_resting_window"


def test_hr_resting_alert_skips_with_no_samples(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.smart_alerts.alerts.maybe_alert") as mock:
        assert smart_alerts.check_hr_while_resting(cfg) is False
    assert not mock.called
