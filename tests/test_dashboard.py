"""Tests for the dashboard data helpers (no FastAPI needed)."""
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import dashboard, db


@pytest.fixture
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        yield p


def test_hr_recent_returns_only_recent_rows(tmpdb):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(tmpdb) as conn:
        # 3 fresh rows, 1 old row outside the 6h window
        for i, offset_min in enumerate([1, 30, 120, 60 * 24]):
            ts = (now - timedelta(minutes=offset_min)).isoformat(timespec="seconds")
            conn.execute("INSERT INTO hr_realtime (ts, bpm) VALUES (?, ?)", (ts, 70 + i))

    out = dashboard._hr_recent(tmpdb, hours=6)
    assert len(out) == 3
    # Sorted ascending
    assert out[0]["t"] < out[-1]["t"]


def test_hrv_recent_returns_rows(tmpdb):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(tmpdb) as conn:
        for i in range(5):
            ts = (now - timedelta(hours=i)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO hrv (ts, rmssd_ms, rr_count) VALUES (?, ?, ?)",
                (ts, 40 + i, 30),
            )
    out = dashboard._hrv_recent(tmpdb, days=7)
    assert len(out) == 5
    assert all("v" in p and "t" in p for p in out)


def test_daily_table_html_empty(tmpdb):
    html = dashboard._daily_table_html([])
    assert "No daily summaries yet" in html


def test_daily_table_html_with_rows():
    rows = [
        {
            "date": "2026-04-01",
            "resting_hr": 58,
            "max_hr": 165,
            "steps": 9214,
            "sleep_seconds": 27000,
            "stress_avg": 31,
            "hrv_overnight": 48,
        }
    ]
    html = dashboard._daily_table_html(rows)
    assert "<table>" in html
    assert "2026-04-01" in html
    assert "58" in html
    assert "7h30m" in html
