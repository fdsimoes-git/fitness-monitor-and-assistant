"""SQLite schema and write helpers."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
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
    message     TEXT,                  -- the human-readable text that was sent
    payload     TEXT,                  -- JSON string with details
    sent_ok     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_kind ON alerts(kind);

CREATE TABLE IF NOT EXISTS hrv (
    ts          TEXT NOT NULL,         -- ISO8601 UTC, end of the window
    rmssd_ms    REAL NOT NULL,
    rr_count    INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'ble'
);
CREATE INDEX IF NOT EXISTS idx_hrv_ts ON hrv(ts);

CREATE TABLE IF NOT EXISTS activities (
    activity_id     TEXT PRIMARY KEY, -- Garmin activity ID
    date            TEXT NOT NULL,    -- YYYY-MM-DD local
    activity_type   TEXT,
    name            TEXT,
    duration_s      INTEGER,
    distance_m      REAL,
    avg_hr          INTEGER,
    max_hr          INTEGER,
    calories        INTEGER,
    training_effect REAL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS meals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at       TEXT NOT NULL,    -- ISO8601 UTC, when row was created
    meal_time       TEXT NOT NULL,    -- ISO8601 UTC, actual meal time
    description     TEXT NOT NULL,
    source          TEXT NOT NULL,    -- 'photo' | 'barcode' | 'manual'
    barcode         TEXT,
    kcal            REAL,
    protein_g       REAL,
    carbs_g         REAL,
    fat_g           REAL,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_meals_meal_time ON meals(meal_time);
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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for DBs created before a column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "message" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN message TEXT")
        log.info("Migrated: added alerts.message column")


def insert_hr(db_path: Path, bpm: int, rr_ms: list[int] | None = None) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    rr_str = ",".join(str(r) for r in rr_ms) if rr_ms else None
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO hr_realtime (ts, bpm, rr_ms) VALUES (?, ?, ?)",
            (ts, bpm, rr_str),
        )


def insert_hrv(db_path: Path, rmssd_ms: float, rr_count: int, source: str = "ble") -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO hrv (ts, rmssd_ms, rr_count, source) VALUES (?, ?, ?, ?)",
            (ts, float(rmssd_ms), int(rr_count), source),
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


def upsert_activity(db_path: Path, fields: dict) -> None:
    """Insert or update one activity. Required key: `activity_id`. Other keys mirror columns."""
    cols = [
        "activity_id", "date", "activity_type", "name",
        "duration_s", "distance_m", "avg_hr", "max_hr",
        "calories", "training_effect",
    ]
    values = {c: fields.get(c) for c in cols}
    if not values["activity_id"]:
        raise ValueError("upsert_activity requires activity_id")
    values["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    placeholders = ", ".join(f":{k}" for k in values)
    columns_sql = ", ".join(values.keys())
    update_sql = ", ".join(
        f"{c}=excluded.{c}" for c in values.keys() if c != "activity_id"
    )

    with connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO activities ({columns_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {update_sql}",
            values,
        )


def recent_activities(db_path: Path, days: int = 7) -> list[dict]:
    """Return activities with `date` within the last `days` days, newest first."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM activities WHERE date >= ? ORDER BY date DESC, activity_id DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def activities_for_date(db_path: Path, date_iso: str) -> list[dict]:
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM activities WHERE date = ? ORDER BY activity_id DESC",
            (date_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_meal(db_path: Path, meal: dict) -> int:
    """Insert one meal row. Returns the new row id.

    Required keys: `description`, `source`. `meal_time` defaults to now (UTC),
    `logged_at` is always now. Other macro/kcal fields are optional.
    """
    if not meal.get("description"):
        raise ValueError("insert_meal requires description")
    if not meal.get("source"):
        raise ValueError("insert_meal requires source")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = {
        "logged_at": now,
        "meal_time": meal.get("meal_time") or now,
        "description": meal["description"],
        "source": meal["source"],
        "barcode": meal.get("barcode"),
        "kcal": meal.get("kcal"),
        "protein_g": meal.get("protein_g"),
        "carbs_g": meal.get("carbs_g"),
        "fat_g": meal.get("fat_g"),
        "raw_json": meal.get("raw_json"),
    }
    columns_sql = ", ".join(values.keys())
    placeholders = ", ".join(f":{k}" for k in values)
    with connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                f"INSERT INTO meals ({columns_sql}) VALUES ({placeholders})",
                values,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return int(cur.lastrowid)


def meals_for_date(db_path: Path, date_iso: str) -> list[dict]:
    """Return meals whose `meal_time` falls on the given local date, oldest first."""
    start = datetime.combine(date.fromisoformat(date_iso), time(0, 0)).astimezone()
    end = start + timedelta(days=1)
    start_iso = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_iso = end.astimezone(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM meals WHERE meal_time >= ? AND meal_time < ? "
            "ORDER BY meal_time ASC, id ASC",
            (start_iso, end_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def calorie_balance_for_date(db_path: Path, date_iso: str) -> dict:
    """Eaten kcal/macros from meals + burned kcal from activities for one local date."""
    meals = meals_for_date(db_path, date_iso)
    eaten = sum(m["kcal"] for m in meals if m.get("kcal") is not None)
    protein = sum(m["protein_g"] for m in meals if m.get("protein_g") is not None)
    carbs = sum(m["carbs_g"] for m in meals if m.get("carbs_g") is not None)
    fat = sum(m["fat_g"] for m in meals if m.get("fat_g") is not None)

    activities = activities_for_date(db_path, date_iso)
    burned = sum(a["calories"] for a in activities if a.get("calories") is not None)

    return {
        "date": date_iso,
        "eaten_kcal": round(eaten, 1) if eaten else 0.0,
        "burned_kcal": int(burned),
        "balance_kcal": round(eaten - burned, 1),
        "protein_g": round(protein, 1) if protein else 0.0,
        "carbs_g": round(carbs, 1) if carbs else 0.0,
        "fat_g": round(fat, 1) if fat else 0.0,
        "meal_count": len(meals),
    }


def log_alert(
    db_path: Path,
    kind: str,
    payload: str,
    sent_ok: bool,
    message: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO alerts (ts, kind, message, payload, sent_ok) VALUES (?, ?, ?, ?, ?)",
            (ts, kind, message, payload, int(sent_ok)),
        )


def recent_alerts(db_path: Path, limit: int = 10) -> list[dict]:
    """Return most recent alerts, newest first."""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, ts, kind, message, payload, sent_ok FROM alerts "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def last_alert_ts(db_path: Path, kind: str) -> datetime | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT ts FROM alerts WHERE kind = ? AND sent_ok = 1 ORDER BY ts DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def recent_resting_hr(db_path: Path, days: int) -> list[tuple[str, int]]:
    """Return [(date, resting_hr)] for the last `days` days, oldest first.

    Skips days with NULL resting_hr.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date, resting_hr FROM daily_summary "
            "WHERE resting_hr IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return [(r[0], int(r[1])) for r in reversed(rows)]


def recent_hrv_overnight(db_path: Path, days: int) -> list[tuple[str, int]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date, hrv_overnight FROM daily_summary "
            "WHERE hrv_overnight IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return [(r[0], int(r[1])) for r in reversed(rows)]


def recent_daily_metrics(db_path: Path, days: int) -> list[dict]:
    """Return last `days` rows of (date, resting_hr, hrv_overnight) — oldest first."""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, resting_hr, hrv_overnight FROM daily_summary "
            "ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def avg_hr_between(db_path: Path, start_iso: str, end_iso: str) -> tuple[float, int] | None:
    """(avg_bpm, sample_count) from hr_realtime within an ISO UTC window. None if no rows."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT AVG(bpm), COUNT(*) FROM hr_realtime WHERE ts >= ? AND ts < ?",
            (start_iso, end_iso),
        ).fetchone()
    if not row or not row[1]:
        return None
    return (float(row[0]), int(row[1]))


def prune_old_data(con: sqlite3.Connection, days: int = 90) -> dict[str, int]:
    """Delete rows older than `days` from hr_realtime, hrv, activities; then VACUUM.

    daily_summary and alerts are kept forever (one row per day, tiny).
    Returns a dict of table_name -> rows deleted.
    """
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    deleted: dict[str, int] = {}
    for table in ("hr_realtime", "hrv"):
        cur = con.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff_ts,))
        deleted[table] = cur.rowcount or 0
    cur = con.execute("DELETE FROM activities WHERE date < ?", (cutoff_date,))
    deleted["activities"] = cur.rowcount or 0
    cur = con.execute("DELETE FROM meals WHERE meal_time < ?", (cutoff_ts,))
    deleted["meals"] = cur.rowcount or 0
    con.execute("VACUUM")
    log.info(
        "Pruned rows older than %s (cutoff_ts=%s cutoff_date=%s): "
        "hr_realtime=%d hrv=%d activities=%d meals=%d",
        f"{days}d", cutoff_ts, cutoff_date,
        deleted["hr_realtime"], deleted["hrv"], deleted["activities"], deleted["meals"],
    )
    return deleted


def daily_summary_for(db_path: Path, date_iso: str) -> dict | None:
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (date_iso,)
        ).fetchone()
    return dict(row) if row else None
