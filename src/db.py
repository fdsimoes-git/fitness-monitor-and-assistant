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
    fiber_g         REAL,
    sugars_g        REAL,
    saturated_fat_g REAL,
    sodium_mg       REAL,
    food_category   TEXT,             -- coarse category from OFF pnns_groups_2
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_meals_meal_time ON meals(meal_time);
"""


def local_today() -> date:
    """The Pi's local 'today'.

    The codebase has a deliberate split: timestamps are stored in UTC (they
    represent the absolute moment something happened — never lie about
    that), but day-bucketing for queries follows the user's local timezone.
    Most date columns in this DB are local-formatted:
    - `daily_summary.date` — written by the poller as `date.today().isoformat()`
    - `activities.date` — derived from Garmin's `startTimeLocal[:10]`
    Helpers that compare against those columns therefore must use local
    "today", not UTC's. UTC and local can disagree by a calendar day around
    midnight, which previously caused the dashboard's 7-day balance and
    year heatmap to show next-day entries during late evening on UTC-3+.
    """
    return date.today()


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

    meal_cols = {r[1] for r in conn.execute("PRAGMA table_info(meals)").fetchall()}
    for col, decl in (
        ("fiber_g", "REAL"),
        ("sugars_g", "REAL"),
        ("saturated_fat_g", "REAL"),
        ("sodium_mg", "REAL"),
        ("food_category", "TEXT"),
    ):
        if col not in meal_cols:
            conn.execute(f"ALTER TABLE meals ADD COLUMN {col} {decl}")
            log.info("Migrated: added meals.%s column", col)


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
    # `activities.date` is local-formatted (Garmin's startTimeLocal[:10]),
    # so the cutoff must be Pi-local — not UTC.
    cutoff = (local_today() - timedelta(days=days)).isoformat()
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
    # Normalize meal_time to UTC ISO so the column shape is consistent
    # regardless of whether the model passed a tz-aware ISO or a naive one
    # (Claude tool calls sometimes return naive timestamps because the
    # system prompt frames "today" in Pi-local terms). _iso_to_utc_aware
    # treats naive as local — the user's likely intent — and converts.
    raw_meal_time = meal.get("meal_time") or now
    try:
        meal_time = _iso_to_utc_aware(raw_meal_time).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        meal_time = now  # fall back rather than blocking the insert
    values = {
        "logged_at": now,
        "meal_time": meal_time,
        "description": meal["description"],
        "source": meal["source"],
        "barcode": meal.get("barcode"),
        "kcal": meal.get("kcal"),
        "protein_g": meal.get("protein_g"),
        "carbs_g": meal.get("carbs_g"),
        "fat_g": meal.get("fat_g"),
        "fiber_g": meal.get("fiber_g"),
        "sugars_g": meal.get("sugars_g"),
        "saturated_fat_g": meal.get("saturated_fat_g"),
        "sodium_mg": meal.get("sodium_mg"),
        "food_category": meal.get("food_category"),
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


def get_meal_by_id(db_path: Path, meal_id: int) -> dict | None:
    """Fetch a single meal row by primary key."""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM meals WHERE id = ?", (int(meal_id),)).fetchone()
    return dict(row) if row else None


# Columns on `meals` that the chat-driven edit tool is allowed to mutate.
# Public so the LLM tool dispatcher can filter against the same set BEFORE
# stashing a proposal — otherwise the proposal can claim it'll edit fields
# (source, raw_json, …) that update_meal then silently drops.
EDITABLE_MEAL_COLUMNS = {
    "description", "kcal", "protein_g", "carbs_g", "fat_g",
    "fiber_g", "sugars_g", "saturated_fat_g", "sodium_mg",
    "food_category", "meal_time",
}


def update_meal(db_path: Path, meal_id: int, fields: dict) -> bool:
    """Partial update of a meal row. Only columns in `EDITABLE_MEAL_COLUMNS` are
    applied; unknown keys are silently dropped (the tool layer is the validator).

    Returns True if a row was updated, False if no row matched the id or no
    valid fields were supplied.
    """
    payload = {k: v for k, v in (fields or {}).items() if k in EDITABLE_MEAL_COLUMNS}
    if not payload:
        return False
    set_sql = ", ".join(f"{k} = :{k}" for k in payload)
    payload["meal_id"] = int(meal_id)
    with connect(db_path) as conn:
        cur = conn.execute(f"UPDATE meals SET {set_sql} WHERE id = :meal_id", payload)
    return (cur.rowcount or 0) > 0


def delete_meal(db_path: Path, meal_id: int) -> bool:
    """Delete one meal row. Returns True if a row was removed."""
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM meals WHERE id = ?", (int(meal_id),))
    return (cur.rowcount or 0) > 0


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


def calorie_balance_for_date(db_path: Path, date_iso: str, cfg=None) -> dict:
    """Eaten kcal/macros from meals + burned kcal from activities/BMR/steps for one local date.
    
    Args:
        db_path: Path to SQLite database
        date_iso: Date in YYYY-MM-DD format
        cfg: Config object with biometric data (age, height, weight, sex). If None, only activities counted.
    """
    meals = meals_for_date(db_path, date_iso)
    eaten = sum(m["kcal"] for m in meals if m.get("kcal") is not None)
    protein = sum(m["protein_g"] for m in meals if m.get("protein_g") is not None)
    carbs = sum(m["carbs_g"] for m in meals if m.get("carbs_g") is not None)
    fat = sum(m["fat_g"] for m in meals if m.get("fat_g") is not None)

    activities = activities_for_date(db_path, date_iso)
    activity_burned = sum(a["calories"] for a in activities if a.get("calories") is not None)

    # Calculate BMR (Mifflin-St Jeor) and step calories if config available
    bmr_kcal = 0
    steps_burned = 0
    if cfg:
        # BMR formula: for men: (10×weight) + (6.25×height) - (5×age) + 5
        #              for women: (10×weight) + (6.25×height) - (5×age) - 161
        if cfg.user_sex.lower() == "male":
            bmr_kcal = (10 * cfg.user_weight_kg) + (6.25 * cfg.user_height_cm) - (5 * cfg.user_age) + 5
        else:
            bmr_kcal = (10 * cfg.user_weight_kg) + (6.25 * cfg.user_height_cm) - (5 * cfg.user_age) - 161
        
        # Get step count for the date and estimate calories (~0.048 kcal per step for 75kg person)
        # Adjust based on actual weight: kcal_per_step = 0.048 * (weight / 75)
        summary = daily_summary_for(db_path, date_iso)
        if summary and summary.get("steps"):
            kcal_per_step = 0.048 * (cfg.user_weight_kg / 75.0)
            steps_burned = summary["steps"] * kcal_per_step

    total_burned = activity_burned + bmr_kcal + steps_burned

    return {
        "date": date_iso,
        "eaten_kcal": round(eaten, 1) if eaten else 0.0,
        "burned_kcal": int(activity_burned),
        "bmr_kcal": int(bmr_kcal),
        "steps_burned_kcal": int(steps_burned),
        "total_burned_kcal": int(total_burned),
        "balance_kcal": round(eaten - total_burned, 1),
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


def prune_old_data(con: sqlite3.Connection, days: int = 90) -> dict[str, int]:
    """Delete rows older than `days` from activities and meals; then VACUUM.

    daily_summary and alerts are kept forever (one row per day, tiny).
    Returns a dict of table_name -> rows deleted.
    """
    # `meal_time` is stored as a UTC ISO timestamp → cutoff must be UTC.
    # `activities.date` is local-formatted → cutoff must be Pi-local.
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    cutoff_date = (local_today() - timedelta(days=days)).isoformat()
    deleted: dict[str, int] = {}
    cur = con.execute("DELETE FROM activities WHERE date < ?", (cutoff_date,))
    deleted["activities"] = cur.rowcount or 0
    cur = con.execute("DELETE FROM meals WHERE meal_time < ?", (cutoff_ts,))
    deleted["meals"] = cur.rowcount or 0
    con.execute("VACUUM")
    log.info(
        "Pruned rows older than %s (cutoff_ts=%s cutoff_date=%s): "
        "activities=%d meals=%d",
        f"{days}d", cutoff_ts, cutoff_date,
        deleted["activities"], deleted["meals"],
    )
    return deleted


# ──────────────────────────────────────────────────────────────────────────────
# Nutrition compute helpers
# ──────────────────────────────────────────────────────────────────────────────

# Categories OFF labels as low-quality / discretionary calories. Anything not in
# this set counts toward "% calories from whole foods".
_DISCRETIONARY_CATEGORIES = {
    "sugary snacks",
    "salty snacks",
    "sweetened beverages",
    "alcoholic beverages",
    "fats and sauces",
    "processed meat",
}


def protein_target_g(cfg) -> float:
    """Daily protein target in grams: weight_kg × cfg.protein_target_g_per_kg."""
    return float(cfg.user_weight_kg) * float(cfg.protein_target_g_per_kg)


def fiber_target_g(cfg) -> float:
    """Daily fiber target: 14 g per 1000 kcal of daily target (Institute of Medicine)."""
    return 14.0 * (float(cfg.kcal_target) / 1000.0)


def sodium_target_mg(cfg=None) -> int:
    """WHO upper limit for healthy adults: 2300 mg/day. cfg unused, kept for API parity."""
    return 2300


def energy_availability(db_path: Path, date_iso: str, cfg) -> dict:
    """RED-S energy availability: (eaten − activity_kcal) / lean_body_mass_kg.

    Returns {ea_kcal_per_kg, eaten_kcal, activity_kcal, lbm_kg, status}.
    `status`: 'optimal' (>= 45), 'low' (30–45), 'red_s' (< 30 — IOC 2018).
    Returns status='unknown' if no meals were logged that day.
    """
    bal = calorie_balance_for_date(db_path, date_iso, cfg=cfg)
    eaten = float(bal["eaten_kcal"])
    activity_kcal = float(bal["burned_kcal"])  # activity-only burn
    lbm_kg = float(cfg.user_weight_kg) * 0.85  # rough estimate; could refine with body-fat input

    if bal["meal_count"] == 0 or lbm_kg <= 0:
        return {
            "ea_kcal_per_kg": None,
            "eaten_kcal": eaten,
            "activity_kcal": activity_kcal,
            "lbm_kg": lbm_kg,
            "status": "unknown",
        }
    ea = (eaten - activity_kcal) / lbm_kg
    if ea < 30:
        status = "red_s"
    elif ea < 45:
        status = "low"
    else:
        status = "optimal"
    return {
        "ea_kcal_per_kg": round(ea, 1),
        "eaten_kcal": eaten,
        "activity_kcal": activity_kcal,
        "lbm_kg": round(lbm_kg, 1),
        "status": status,
    }


def whole_food_pct(db_path: Path, date_iso: str) -> float | None:
    """Fraction of eaten kcal from non-discretionary categories. None if no meals.

    Meals with no `food_category` are counted as whole-food (manual entries are
    typically real meals, not packaged junk). To penalize unknown sources,
    require categories.
    """
    meals = meals_for_date(db_path, date_iso)
    total = sum(m["kcal"] for m in meals if m.get("kcal") is not None)
    if not total:
        return None
    discretionary = sum(
        m["kcal"]
        for m in meals
        if m.get("kcal") is not None
        and (m.get("food_category") or "").strip().lower() in _DISCRETIONARY_CATEGORIES
    )
    return round((total - discretionary) / total, 3)


def _iso_to_utc_aware(iso_str: str) -> datetime:
    """Parse an ISO timestamp string to a UTC-aware datetime.

    The schema declares `meal_time` as UTC ISO, but in practice Claude tool
    calls sometimes return naive timestamps (no tz suffix) because the model
    is reasoning in Pi-local time under the "Today is …" system prompt.
    Treat naive values as local time (the most likely intent), then convert
    to UTC. This makes downstream math (`datetime.now(utc) - x`) safe
    regardless of which shape is in storage.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret naive as system local
    return dt.astimezone(timezone.utc)


def meal_timing_summary(db_path: Path, date_iso: str) -> dict | None:
    """First/last meal of the day and the gap from last meal to now.

    Returns None if no meals on `date_iso`.
    """
    meals = meals_for_date(db_path, date_iso)
    if not meals:
        return None
    first_dt = _iso_to_utc_aware(meals[0]["meal_time"]).astimezone()  # display in local
    last_dt = _iso_to_utc_aware(meals[-1]["meal_time"]).astimezone()
    eating_window_h = round((last_dt - first_dt).total_seconds() / 3600.0, 2)
    fasting_h_since_last = round(
        (datetime.now(timezone.utc) - _iso_to_utc_aware(meals[-1]["meal_time"])).total_seconds()
        / 3600.0,
        2,
    )
    return {
        "first_meal_local": first_dt.strftime("%H:%M"),
        "last_meal_local": last_dt.strftime("%H:%M"),
        "eating_window_h": eating_window_h,
        "hours_since_last_meal": fasting_h_since_last,
        "meal_count": len(meals),
    }


def recent_calorie_balance(db_path: Path, days: int, cfg) -> list[dict]:
    """[{date, balance_kcal}] for the last `days` calendar days, oldest first.

    Pulls the per-day balance via `calorie_balance_for_date` so the math stays
    consistent with the CLI / dashboard output. Anchored on Pi-local "today"
    to match the rest of the codebase (see `local_today`).
    """
    today = local_today()
    out: list[dict] = []
    for offset in range(days - 1, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        bal = calorie_balance_for_date(db_path, d, cfg=cfg)
        out.append({"date": d, "balance_kcal": bal["balance_kcal"]})
    return out


def daily_summary_for(db_path: Path, date_iso: str) -> dict | None:
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (date_iso,)
        ).fetchone()
    return dict(row) if row else None


# ──────────────────────────────────────────────────────────────────────────────
# Training-intelligence metrics
# ──────────────────────────────────────────────────────────────────────────────

# Composite-readiness component weights (must sum to 1.0).
_READINESS_WEIGHTS = {"hrv": 0.50, "rhr": 0.20, "sleep": 0.20, "bb": 0.10}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _hrmax(cfg) -> int:
    return cfg.user_hrmax if cfg.user_hrmax > 0 else (220 - cfg.user_age)


def _last_n_summaries(db_path: Path, end_date_iso: str, days: int) -> list[dict]:
    """Last `days` daily_summary rows ending on `end_date_iso` (inclusive), oldest first."""
    end = date.fromisoformat(end_date_iso)
    start = (end - timedelta(days=days - 1)).isoformat()
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM daily_summary WHERE date >= ? AND date <= ? ORDER BY date ASC",
            (start, end_date_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def composite_readiness(db_path: Path, date_iso: str, cfg) -> dict:
    """0–100 readiness score blending HRV, RHR, sleep, body battery.

    Each component is normalized to [-1, +1] vs its 7-day baseline (or vs target
    for sleep) and weighted per `_READINESS_WEIGHTS`. Missing components have
    their weight redistributed across the survivors so the score still spans
    [0, 100]. Returns {score, band, components} where band is 'high'/'medium'/'low'.

    Returns score=None if no usable components are available for `date_iso`.
    """
    today = daily_summary_for(db_path, date_iso) or {}
    baseline = _last_n_summaries(
        db_path,
        (date.fromisoformat(date_iso) - timedelta(days=1)).isoformat(),
        7,
    )

    def _mean(rows: list[dict], key: str) -> float | None:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    components: dict[str, float | None] = {"hrv": None, "rhr": None, "sleep": None, "bb": None}

    hrv_today = today.get("hrv_overnight")
    hrv_base = _mean(baseline, "hrv_overnight")
    if hrv_today is not None and hrv_base and hrv_base > 0:
        components["hrv"] = _clamp((hrv_today - hrv_base) / hrv_base, -1.0, 1.0)

    rhr_today = today.get("resting_hr")
    rhr_base = _mean(baseline, "resting_hr")
    if rhr_today is not None and rhr_base and rhr_base > 0:
        # lower RHR is better — negate the deviation
        components["rhr"] = _clamp(-(rhr_today - rhr_base) / rhr_base, -1.0, 1.0)

    sleep_today = today.get("sleep_seconds")
    if sleep_today is not None and cfg.sleep_target_hours > 0:
        target_s = cfg.sleep_target_hours * 3600.0
        components["sleep"] = _clamp((sleep_today - target_s) / target_s, -1.0, 1.0)

    bb_today = today.get("body_battery")
    if bb_today is not None:
        # 50 ≈ neutral, 100 = best, 0 = worst.
        components["bb"] = _clamp((bb_today - 50.0) / 50.0, -1.0, 1.0)

    available = {k: v for k, v in components.items() if v is not None}
    if not available:
        return {"score": None, "band": None, "components": components}

    weight_sum = sum(_READINESS_WEIGHTS[k] for k in available)
    weighted = sum(_READINESS_WEIGHTS[k] * v for k, v in available.items()) / weight_sum
    score = round(50 + 50 * weighted)
    if score >= 80:
        band = "high"
    elif score >= 60:
        band = "medium"
    else:
        band = "low"
    return {"score": score, "band": band, "components": components}


def daily_readiness_history(db_path: Path, days: int, cfg) -> list[dict]:
    """[{date, score}] for the last `days` days ending today, oldest first.

    Skips days where the score is None (no data) — caller renders empty cells.
    Anchored on Pi-local "today" so the heatmap's most recent cell stays
    aligned with what the user calls "today" (see `local_today`).
    """
    today = local_today()
    out: list[dict] = []
    for offset in range(days - 1, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        r = composite_readiness(db_path, d, cfg)
        out.append({"date": d, "score": r["score"], "band": r["band"]})
    return out


def _daily_activity_load(db_path: Path, end_date_iso: str, days: int) -> dict[str, int]:
    """Date-keyed sum of activity calories over the window. Missing days = 0."""
    end = date.fromisoformat(end_date_iso)
    start = (end - timedelta(days=days - 1)).isoformat()
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date, COALESCE(SUM(calories), 0) FROM activities "
            "WHERE date >= ? AND date <= ? GROUP BY date",
            (start, end_date_iso),
        ).fetchall()
    by_date = {r[0]: int(r[1] or 0) for r in rows}
    out: dict[str, int] = {}
    for offset in range(days):
        d = (end - timedelta(days=days - 1 - offset)).isoformat()
        out[d] = by_date.get(d, 0)
    return out


def acwr(db_path: Path, date_iso: str) -> dict:
    """Acute:Chronic Workload Ratio = mean(7d load) / mean(28d load).

    Sweet spot 0.8–1.3 (Gabbett 2016). >1.5 = elevated injury risk.
    Returns {ratio, acute_avg, chronic_avg, band}. ratio=None if insufficient data.
    """
    loads_28 = list(_daily_activity_load(db_path, date_iso, 28).values())
    if len(loads_28) < 28:
        return {"ratio": None, "acute_avg": None, "chronic_avg": None, "band": None}
    chronic = sum(loads_28) / 28.0
    acute = sum(loads_28[-7:]) / 7.0
    if chronic <= 0:
        ratio = None
        band = None
    else:
        ratio = acute / chronic
        if ratio < 0.8:
            band = "undertrained"
        elif ratio <= 1.3:
            band = "optimal"
        elif ratio <= 1.5:
            band = "caution"
        else:
            band = "high_risk"
    return {
        "ratio": round(ratio, 2) if ratio is not None else None,
        "acute_avg": round(acute, 1),
        "chronic_avg": round(chronic, 1),
        "band": band,
    }


def training_monotony(db_path: Path, date_iso: str) -> dict:
    """Foster's monotony: mean(daily load) / std(daily load) over last 7 days.

    > 2.0 is generally considered monotonous (overuse risk). Returns
    {monotony, band}. monotony=None if std is 0 (constant load) or fewer than
    7 days of data.
    """
    loads = list(_daily_activity_load(db_path, date_iso, 7).values())
    if len(loads) < 7:
        return {"monotony": None, "band": None}
    mean = sum(loads) / 7.0
    if mean == 0:
        return {"monotony": None, "band": "rest"}  # nothing trained; still meaningful
    variance = sum((x - mean) ** 2 for x in loads) / 7.0
    std = variance ** 0.5
    if std == 0:
        return {"monotony": None, "band": "monotonous"}
    monotony = mean / std
    if monotony >= 2.0:
        band = "monotonous"
    elif monotony >= 1.5:
        band = "elevated"
    else:
        band = "varied"
    return {"monotony": round(monotony, 2), "band": band}


def z2_minutes_for_week(db_path: Path, end_date_iso: str, cfg) -> dict:
    """Sum activity minutes whose avg HR sits in 60–70% of HRmax for the trailing 7 days.

    Z2 (aerobic base) is the gold standard for endurance development. Goal: 150 min/week.
    Returns {minutes, goal_minutes, hrmax, lower_bpm, upper_bpm, by_day}.
    """
    hrmax = _hrmax(cfg)
    lower = round(0.60 * hrmax)
    upper = round(0.70 * hrmax)
    end = date.fromisoformat(end_date_iso)
    start = (end - timedelta(days=6)).isoformat()
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date, COALESCE(SUM(duration_s), 0) FROM activities "
            "WHERE date >= ? AND date <= ? "
            "AND avg_hr >= ? AND avg_hr <= ? "
            "GROUP BY date",
            (start, end_date_iso, lower, upper),
        ).fetchall()
    by_day_seconds = {r[0]: int(r[1] or 0) for r in rows}
    by_day = []
    total_s = 0
    for offset in range(7):
        d = (end - timedelta(days=6 - offset)).isoformat()
        secs = by_day_seconds.get(d, 0)
        by_day.append({"date": d, "minutes": secs // 60})
        total_s += secs
    return {
        "minutes": total_s // 60,
        "goal_minutes": 150,
        "hrmax": hrmax,
        "lower_bpm": lower,
        "upper_bpm": upper,
        "by_day": by_day,
    }


def sleep_debt(db_path: Path, end_date_iso: str, cfg) -> dict:
    """Cumulative (target − actual) sleep over the trailing 7 days, in hours.

    Positive total = net deficit. Negative = net surplus. by_day items report
    the *signed* deficit per day (positive when short, negative when over).
    """
    rows = _last_n_summaries(db_path, end_date_iso, 7)
    target_s = cfg.sleep_target_hours * 3600.0
    by_date = {r["date"]: r.get("sleep_seconds") for r in rows}
    by_day = []
    total_s = 0.0
    end = date.fromisoformat(end_date_iso)
    for offset in range(7):
        d = (end - timedelta(days=6 - offset)).isoformat()
        actual = by_date.get(d)
        if actual is None:
            by_day.append({"date": d, "deficit_h": None})
            continue
        deficit = target_s - actual
        total_s += deficit
        by_day.append({"date": d, "deficit_h": round(deficit / 3600.0, 2)})
    return {
        "total_h": round(total_s / 3600.0, 2),
        "target_h": cfg.sleep_target_hours,
        "by_day": by_day,
    }
