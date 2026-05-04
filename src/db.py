"""SQLite schema and write helpers."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_realtime (
    ts          TEXT NOT NULL,         -- ISO8601 UTC
    bpm         INTEGER NOT NULL,
    rr_ms       TEXT,                  -- comma-separated RR intervals in ms (optional)
    source      TEXT NOT NULL DEFAULT 'ble'
);
CREATE INDEX IF NOT EXISTS idx_hr_ts ON hr_realtime(ts);

CREATE TABLE IF NOT EXISTS daily_summary (
    date            TEXT PRIMARY KEY,  -- YYYY-MM-DD
    resting_hr      INTEGER,
    max_hr          INTEGER,
    avg_hr          INTEGER,
    steps           INTEGER,
    sleep_seconds   INTEGER,
    stress_avg      INTEGER,
    body_battery    INTEGER,
    hrv_overnight   INTEGER,
    raw_json        TEXT,              -- full payload for later re-analysis
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,         -- e.g. 'hr_spike', 'resting_hr_high'
    payload     TEXT,                  -- JSON string with details
    sent_ok     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_kind ON alerts(kind);
"""


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    log.info("Initialising DB at %s", db_path)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def insert_hr(db_path: Path, bpm: int, rr_ms: list[int] | None = None) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    rr_str = ",".join(str(r) for r in rr_ms) if rr_ms else None
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO hr_realtime (ts, bpm, rr_ms) VALUES (?, ?, ?)",
            (ts, bpm, rr_str),
        )


def upsert_daily_summary(db_path: Path, date: str, fields: dict) -> None:
    """fields: dict of column_name -> value. Unknown keys are ignored."""
    cols = [
        "resting_hr", "max_hr", "avg_hr", "steps",
        "sleep_seconds", "stress_avg", "body_battery", "hrv_overnight", "raw_json",
    ]
    values = {c: fields.get(c) for c in cols}
    values["date"] = date
    values["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    placeholders = ", ".join(f":{k}" for k in values)
    columns_sql = ", ".join(values.keys())
    update_sql = ", ".join(f"{c}=excluded.{c}" for c in values.keys() if c != "date")

    with connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO daily_summary ({columns_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {update_sql}",
            values,
        )


def log_alert(db_path: Path, kind: str, payload: str, sent_ok: bool) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO alerts (ts, kind, payload, sent_ok) VALUES (?, ?, ?, ?)",
            (ts, kind, payload, int(sent_ok)),
        )


def last_alert_ts(db_path: Path, kind: str) -> datetime | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT ts FROM alerts WHERE kind = ? AND sent_ok = 1 ORDER BY ts DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None
