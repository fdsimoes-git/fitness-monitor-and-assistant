"""Tests for the nutrition compute helpers added in Phase 3."""
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import db
from src.config import Config


def _make_cfg(db_path: Path, **overrides) -> Config:
    base = dict(
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
        anthropic_api_key="",
        claude_oauth_token="",
        claude_model="claude-sonnet-4-6",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        yield p


def test_protein_target_scales_with_weight(tmpdb):
    assert db.protein_target_g(_make_cfg(tmpdb, user_weight_kg=80)) == pytest.approx(128.0)
    assert db.protein_target_g(_make_cfg(tmpdb, user_weight_kg=60)) == pytest.approx(96.0)


def test_protein_target_respects_per_kg_override(tmpdb):
    cfg = _make_cfg(tmpdb, user_weight_kg=70, protein_target_g_per_kg=2.0)
    assert db.protein_target_g(cfg) == pytest.approx(140.0)


def test_fiber_target_scales_with_kcal(tmpdb):
    assert db.fiber_target_g(_make_cfg(tmpdb, kcal_target=2000)) == pytest.approx(28.0)
    assert db.fiber_target_g(_make_cfg(tmpdb, kcal_target=2500)) == pytest.approx(35.0)


def test_sodium_target_is_who_upper_bound(tmpdb):
    assert db.sodium_target_mg() == 2300


def test_energy_availability_optimal(tmpdb):
    """75 kg × 0.85 = 63.75 kg LBM. Need EA >= 45 kcal/kg → eaten - burned >= 2868."""
    cfg = _make_cfg(tmpdb)
    today = date.today().isoformat()
    db.insert_meal(tmpdb, {"description": "breakfast", "source": "manual", "kcal": 3000.0})
    out = db.energy_availability(tmpdb, today, cfg)
    assert out["status"] == "optimal"
    assert out["ea_kcal_per_kg"] is not None
    assert out["lbm_kg"] == pytest.approx(63.75, abs=0.1)


def test_energy_availability_red_s_warning(tmpdb):
    """Eat little, burn a lot → EA < 30 kcal/kg LBM → 'red_s'."""
    cfg = _make_cfg(tmpdb)
    today = date.today().isoformat()
    db.insert_meal(tmpdb, {"description": "small", "source": "manual", "kcal": 800.0})
    db.upsert_activity(
        tmpdb,
        {"activity_id": "1", "date": today, "calories": 1500},
    )
    out = db.energy_availability(tmpdb, today, cfg)
    assert out["status"] == "red_s"


def test_energy_availability_unknown_when_no_meals(tmpdb):
    cfg = _make_cfg(tmpdb)
    out = db.energy_availability(tmpdb, date.today().isoformat(), cfg)
    assert out["status"] == "unknown"
    assert out["ea_kcal_per_kg"] is None


def test_whole_food_pct_excludes_discretionary(tmpdb):
    today = date.today().isoformat()
    # 200 + 300 = 500 kcal "whole food", 200 kcal sugary snacks → 71.4%
    db.insert_meal(tmpdb, {
        "description": "salmon", "source": "manual", "kcal": 200.0,
        "food_category": "Fish, Meat, Eggs",
    })
    db.insert_meal(tmpdb, {
        "description": "rice", "source": "manual", "kcal": 300.0,
        "food_category": "Cereals",
    })
    db.insert_meal(tmpdb, {
        "description": "candy bar", "source": "barcode", "kcal": 200.0,
        "food_category": "Sugary snacks",
    })
    pct = db.whole_food_pct(tmpdb, today)
    assert pct == pytest.approx(0.714, abs=0.01)


def test_whole_food_pct_none_when_no_meals(tmpdb):
    assert db.whole_food_pct(tmpdb, date.today().isoformat()) is None


def test_whole_food_pct_unknown_category_counts_as_whole(tmpdb):
    """Manual entries usually have no category — count them as whole-food."""
    today = date.today().isoformat()
    db.insert_meal(tmpdb, {"description": "lunch", "source": "manual", "kcal": 500.0})
    assert db.whole_food_pct(tmpdb, today) == 1.0


def test_meal_timing_summary_handles_naive_iso_meal_times(tmpdb):
    """Regression for the dashboard HTTP-500 caused by Claude returning
    tz-less meal_time strings. meal_timing_summary used to subtract a
    UTC-aware now() from a naive fromisoformat() result and raise
    TypeError. Now naive ISOs are treated as local, normalized, and the
    subtraction is safe."""
    today = date.today()
    today_iso = today.isoformat()
    # Insert two meals via raw SQL so we keep the tz-less shape (insert_meal
    # would normalize them on the way in — that's the other half of the fix).
    import sqlite3
    naive_breakfast = f"{today_iso}T08:00:00"
    naive_dinner = f"{today_iso}T19:30:00"
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(tmpdb) as conn:
        for naive in (naive_breakfast, naive_dinner):
            conn.execute(
                "INSERT INTO meals (logged_at, meal_time, description, source, kcal) "
                "VALUES (?, ?, ?, ?, ?)",
                (now_utc, naive, "x", "manual", 100),
            )
        conn.commit()
    # Should not raise.
    out = db.meal_timing_summary(tmpdb, today_iso)
    assert out is not None
    assert out["meal_count"] == 2
    assert out["eating_window_h"] > 0
    assert "hours_since_last_meal" in out


def test_insert_meal_normalizes_naive_meal_time_to_utc(tmpdb):
    """Defense in depth: when Claude tool input comes in with a tz-less ISO,
    insert_meal normalizes to a UTC-aware ISO with a tz suffix so the column
    is always consistent going forward."""
    naive = "2026-05-06T13:28:00"
    db.insert_meal(tmpdb, {
        "description": "lunch",
        "source": "ai",
        "kcal": 600,
        "meal_time": naive,
    })
    import sqlite3
    with sqlite3.connect(tmpdb) as conn:
        rows = conn.execute("SELECT meal_time FROM meals ORDER BY id DESC LIMIT 1").fetchall()
    stored = rows[0][0]
    # Stored value carries a tz suffix — either "+HH:MM" or "Z".
    assert stored.endswith(("+00:00", "Z")) or "+" in stored[10:] or "-" in stored[10:]
    # And it round-trips through the ISO parser as a tz-aware datetime.
    parsed = datetime.fromisoformat(stored)
    assert parsed.tzinfo is not None


def test_meal_timing_summary_returns_none_when_empty(tmpdb):
    assert db.meal_timing_summary(tmpdb, date.today().isoformat()) is None


def test_meal_timing_summary_computes_window_and_count(tmpdb):
    today = date.today()
    today_iso = today.isoformat()
    breakfast = datetime.combine(today, datetime.min.time()).replace(
        hour=8, tzinfo=timezone.utc
    ).isoformat(timespec="seconds")
    dinner = datetime.combine(today, datetime.min.time()).replace(
        hour=20, tzinfo=timezone.utc
    ).isoformat(timespec="seconds")
    db.insert_meal(tmpdb, {
        "description": "breakfast", "source": "manual", "kcal": 400.0, "meal_time": breakfast,
    })
    db.insert_meal(tmpdb, {
        "description": "dinner", "source": "manual", "kcal": 600.0, "meal_time": dinner,
    })
    out = db.meal_timing_summary(tmpdb, today_iso)
    assert out is not None
    assert out["meal_count"] == 2
    assert out["eating_window_h"] == pytest.approx(12.0, abs=0.01)
    assert "hours_since_last_meal" in out


def test_recent_calorie_balance_returns_one_row_per_day(tmpdb):
    cfg = _make_cfg(tmpdb)
    # Production anchors on Pi-local 'today' (db.local_today). Match here
    # so this test isn't UTC-flaky around midnight.
    today = date.today()
    db.insert_meal(tmpdb, {"description": "today's lunch", "source": "manual", "kcal": 600.0})
    out = db.recent_calorie_balance(tmpdb, days=7, cfg=cfg)
    assert len(out) == 7
    assert out[0]["date"] == (today - timedelta(days=6)).isoformat()
    assert out[-1]["date"] == today.isoformat()
    # Today should have a non-zero negative balance (we ate 600, burned via BMR>>that)
    assert out[-1]["balance_kcal"] < 0


def test_get_meal_by_id_round_trip(tmpdb):
    mid = db.insert_meal(tmpdb, {"description": "salmon", "source": "manual", "kcal": 400})
    got = db.get_meal_by_id(tmpdb, mid)
    assert got is not None
    assert got["id"] == mid
    assert got["description"] == "salmon"
    assert got["kcal"] == 400


def test_get_meal_by_id_returns_none_for_missing():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        assert db.get_meal_by_id(p, 99999) is None


def test_update_meal_applies_partial_fields(tmpdb):
    mid = db.insert_meal(tmpdb, {"description": "salmon", "source": "manual", "kcal": 400, "protein_g": 30})
    assert db.update_meal(tmpdb, mid, {"kcal": 380, "protein_g": 32}) is True
    after = db.get_meal_by_id(tmpdb, mid)
    assert after["kcal"] == 380
    assert after["protein_g"] == 32
    assert after["description"] == "salmon"  # untouched


def test_update_meal_drops_unknown_columns(tmpdb):
    """Unknown keys must be silently ignored — the tool layer is the validator."""
    mid = db.insert_meal(tmpdb, {"description": "salmon", "source": "manual", "kcal": 400})
    # `id` and `source` aren't in EDITABLE_MEAL_COLUMNS — should not change.
    db.update_meal(tmpdb, mid, {"id": 9999, "source": "evil", "kcal": 350})
    after = db.get_meal_by_id(tmpdb, mid)
    assert after["id"] == mid
    assert after["source"] == "manual"
    assert after["kcal"] == 350


def test_update_meal_returns_false_when_no_valid_fields(tmpdb):
    mid = db.insert_meal(tmpdb, {"description": "x", "source": "manual", "kcal": 100})
    assert db.update_meal(tmpdb, mid, {"id": 1, "source": "y"}) is False


def test_update_meal_returns_false_for_missing_row(tmpdb):
    assert db.update_meal(tmpdb, 99999, {"kcal": 100}) is False


def test_delete_meal_removes_row(tmpdb):
    mid = db.insert_meal(tmpdb, {"description": "x", "source": "manual", "kcal": 100})
    assert db.delete_meal(tmpdb, mid) is True
    assert db.get_meal_by_id(tmpdb, mid) is None


def test_delete_meal_returns_false_for_missing(tmpdb):
    assert db.delete_meal(tmpdb, 99999) is False


def testlocal_today_helper_returns_local_date():
    """Sanity: local_today() agrees with date.today() right now."""
    assert db.local_today() == date.today()


def test_recent_calorie_balance_anchors_onlocal_today(tmpdb):
    """Last entry in the 7-day balance series must be local today, not UTC."""
    from unittest.mock import patch
    cfg = _make_cfg(tmpdb)
    fake_today = date(2026, 5, 5)  # local
    with patch("src.db.local_today", return_value=fake_today):
        out = db.recent_calorie_balance(tmpdb, days=7, cfg=cfg)
    assert out[-1]["date"] == "2026-05-05"
    assert out[0]["date"] == "2026-04-29"


def test_daily_readiness_history_anchors_onlocal_today(tmpdb):
    from unittest.mock import patch
    cfg = _make_cfg(tmpdb)
    fake_today = date(2026, 5, 5)
    with patch("src.db.local_today", return_value=fake_today):
        out = db.daily_readiness_history(tmpdb, days=3, cfg=cfg)
    assert [r["date"] for r in out] == ["2026-05-03", "2026-05-04", "2026-05-05"]


def test_recent_activities_cutoff_useslocal_today(tmpdb):
    """An activity dated as Pi-local 2026-05-05 must be findable when
    queried at local 2026-05-05, even when UTC has rolled over to 05-06."""
    from unittest.mock import patch
    db.upsert_activity(tmpdb, {
        "activity_id": "a1", "date": "2026-05-05", "activity_type": "running",
        "name": "easy run", "duration_s": 1800, "distance_m": 4000,
        "avg_hr": 130, "max_hr": 150, "calories": 300, "training_effect": 2.5,
    })
    with patch("src.db.local_today", return_value=date(2026, 5, 5)):
        out = db.recent_activities(tmpdb, days=1)
    assert any(a["activity_id"] == "a1" for a in out)


def test_meals_persist_new_columns(tmpdb):
    """Schema additions round-trip through insert_meal + meals_for_date."""
    today = date.today().isoformat()
    db.insert_meal(tmpdb, {
        "description": "yogurt",
        "source": "barcode",
        "barcode": "1234567890",
        "kcal": 120.0,
        "protein_g": 5.0,
        "carbs_g": 18.0,
        "fat_g": 2.0,
        "fiber_g": 1.5,
        "sugars_g": 14.0,
        "saturated_fat_g": 1.2,
        "sodium_mg": 80.0,
        "food_category": "Milk and dairy products",
    })
    rows = db.meals_for_date(tmpdb, today)
    assert len(rows) == 1
    r = rows[0]
    assert r["fiber_g"] == 1.5
    assert r["sugars_g"] == 14.0
    assert r["saturated_fat_g"] == 1.2
    assert r["sodium_mg"] == 80.0
    assert r["food_category"] == "Milk and dairy products"
