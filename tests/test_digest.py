"""Tests for the daily digest builder."""
import tempfile
from datetime import date
from pathlib import Path

import pytest

from src import db, digest
from src.config import Config


def _make_cfg(db_path: Path) -> Config:
    return Config(
        garmin_email="x",
        garmin_password="x",
        garmin_token_dir=Path("/tmp/x"),
        telegram_bot_token="x",
        telegram_chat_id="x",
        db_path=db_path,
        hr_resting_high_bpm=85,
        alert_cooldown_seconds=0,
        poll_interval_minutes=15,
        log_level="INFO",
        user_age=30,
        user_height_cm=175,
        user_weight_kg=75,
        user_sex="male",
        protein_target_g_per_kg=1.6,
        kcal_target=2200,
        sleep_target_hours=8,
        user_hrmax=0,
    )


@pytest.fixture
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        yield p


def test_format_seconds():
    assert digest._format_seconds(None) == "—"
    assert digest._format_seconds(0) == "—"
    assert digest._format_seconds(3600) == "1h00m"
    assert digest._format_seconds(7 * 3600 + 23 * 60) == "7h23m"


def test_build_digest_returns_none_when_empty(tmpdb):
    cfg = _make_cfg(tmpdb)
    assert digest.build_digest(cfg, date(2026, 4, 1)) is None


def test_build_digest_includes_known_fields(tmpdb):
    cfg = _make_cfg(tmpdb)
    db.upsert_daily_summary(
        tmpdb,
        "2026-04-01",
        {
            "resting_hr": 58,
            "max_hr": 165,
            "avg_hr": 72,
            "steps": 9214,
            "sleep_seconds": 27000,
            "stress_avg": 31,
            "body_battery": 70,
            "hrv_overnight": 48,
        },
    )
    text = digest.build_digest(cfg, date(2026, 4, 1))
    assert text is not None
    assert "2026-04-01" in text
    assert "*58*" in text  # resting HR is bold
    assert "9214" in text
    assert "7h30m" in text  # 27000 s formatted
    assert "48 ms" in text


def test_build_digest_handles_partial_data(tmpdb):
    cfg = _make_cfg(tmpdb)
    db.upsert_daily_summary(tmpdb, "2026-04-01", {"resting_hr": 60})
    text = digest.build_digest(cfg, date(2026, 4, 1))
    assert text is not None
    assert "*60*" in text
    assert "—" in text  # missing fields render as em-dash
