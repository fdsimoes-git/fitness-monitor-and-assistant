"""Tests for the training-intelligence helpers added in Phase 4a."""
import tempfile
from datetime import date, timedelta
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


def _fill_summaries(tmpdb, end: date, days: int, **per_day):
    """Insert daily_summary rows ending at `end`, oldest first.

    `per_day` is a dict of column → list of values (length=days).
    """
    for offset in range(days):
        d = (end - timedelta(days=days - 1 - offset)).isoformat()
        fields = {k: v[offset] for k, v in per_day.items() if v[offset] is not None}
        db.upsert_daily_summary(tmpdb, d, fields)


def _fill_activities(tmpdb, end: date, days: int, calories: list[int],
                     duration_s: list[int] | None = None,
                     avg_hr: list[int] | None = None):
    for offset in range(days):
        d = (end - timedelta(days=days - 1 - offset)).isoformat()
        if calories[offset] is None:
            continue
        db.upsert_activity(
            tmpdb,
            {
                "activity_id": f"act-{d}",
                "date": d,
                "calories": calories[offset],
                "duration_s": (duration_s or [0] * days)[offset],
                "avg_hr": (avg_hr or [0] * days)[offset],
            },
        )


# ── Composite readiness ──

def test_readiness_high_when_all_metrics_better_than_baseline(tmpdb):
    cfg = _make_cfg(tmpdb)
    today = date.today()
    # 7 baseline days + today: every component clamps to +1 (massive improvement).
    _fill_summaries(
        tmpdb, today, 8,
        hrv_overnight=[40, 42, 41, 43, 42, 40, 41, 100],
        resting_hr=[60, 62, 61, 60, 61, 59, 60, 30],
        sleep_seconds=[7 * 3600] * 7 + [16 * 3600],
        body_battery=[50] * 7 + [100],
    )
    out = db.composite_readiness(tmpdb, today.isoformat(), cfg)
    assert out["score"] is not None
    # HRV/sleep/BB clamp to +1; RHR is bounded by physiology but stays positive
    # → weighted score lands solidly in the "high" band.
    assert out["score"] >= 90
    assert out["band"] == "high"


def test_readiness_low_when_all_metrics_worse(tmpdb):
    cfg = _make_cfg(tmpdb)
    today = date.today()
    _fill_summaries(
        tmpdb, today, 8,
        hrv_overnight=[40, 42, 41, 43, 42, 40, 41, 25],
        resting_hr=[60, 62, 61, 60, 61, 59, 60, 75],
        sleep_seconds=[7 * 3600] * 7 + [4 * 3600],
        body_battery=[50] * 7 + [10],
    )
    out = db.composite_readiness(tmpdb, today.isoformat(), cfg)
    assert out["score"] is not None
    assert out["score"] <= 30
    assert out["band"] == "low"


def test_readiness_returns_none_when_no_data(tmpdb):
    cfg = _make_cfg(tmpdb)
    out = db.composite_readiness(tmpdb, date.today().isoformat(), cfg)
    assert out["score"] is None
    assert out["band"] is None


def test_readiness_handles_partial_components(tmpdb):
    """Only HRV present today → score still computes from available components."""
    cfg = _make_cfg(tmpdb)
    today = date.today()
    _fill_summaries(
        tmpdb, today, 8,
        hrv_overnight=[40, 42, 41, 43, 42, 40, 41, 60],
        resting_hr=[None] * 8,
        sleep_seconds=[None] * 8,
        body_battery=[None] * 8,
    )
    out = db.composite_readiness(tmpdb, today.isoformat(), cfg)
    assert out["score"] is not None
    assert out["components"]["hrv"] is not None
    assert out["components"]["rhr"] is None


# ── ACWR ──

def test_acwr_optimal_band_with_steady_load(tmpdb):
    today = date.today()
    _fill_activities(tmpdb, today, 28, calories=[300] * 28)
    out = db.acwr(tmpdb, today.isoformat())
    assert out["ratio"] == 1.0
    assert out["band"] == "optimal"


def test_acwr_high_risk_when_acute_spikes(tmpdb):
    today = date.today()
    # 3 weeks of 200 kcal/day, 1 week of 800 → ratio ≈ 800/350 ≈ 2.3
    cals = [200] * 21 + [800] * 7
    _fill_activities(tmpdb, today, 28, calories=cals)
    out = db.acwr(tmpdb, today.isoformat())
    assert out["ratio"] is not None
    assert out["ratio"] > 1.5
    assert out["band"] == "high_risk"


def test_acwr_flags_ramp_up_after_inactive_period(tmpdb):
    """21 zero-days + 7 active-days simulates someone restarting training.
    chronic = (0*21 + 300*7)/28 = 75, acute = 300, ratio = 4.0 → high_risk."""
    today = date.today()
    cals = [0] * 21 + [300] * 7
    _fill_activities(tmpdb, today, 28, calories=cals)
    out = db.acwr(tmpdb, today.isoformat())
    assert out["ratio"] is not None
    assert out["ratio"] > 1.5
    assert out["band"] == "high_risk"


def test_acwr_returns_none_when_chronic_load_is_zero(tmpdb):
    today = date.today()
    _fill_activities(tmpdb, today, 28, calories=[0] * 28)
    out = db.acwr(tmpdb, today.isoformat())
    assert out["ratio"] is None
    assert out["chronic_avg"] == 0.0


# ── Training monotony ──

def test_monotony_low_when_load_varies(tmpdb):
    today = date.today()
    # Big variance → low monotony
    _fill_activities(tmpdb, today, 7, calories=[100, 800, 200, 700, 150, 0, 600])
    out = db.training_monotony(tmpdb, today.isoformat())
    assert out["monotony"] is not None
    assert out["band"] == "varied"


def test_monotony_flagged_when_uniform_load(tmpdb):
    today = date.today()
    # Slight noise around constant → high mean/std
    _fill_activities(tmpdb, today, 7, calories=[300, 305, 300, 295, 305, 300, 295])
    out = db.training_monotony(tmpdb, today.isoformat())
    assert out["monotony"] is not None
    assert out["band"] == "monotonous"


def test_monotony_rest_band_when_no_load(tmpdb):
    today = date.today()
    _fill_activities(tmpdb, today, 7, calories=[0] * 7)
    out = db.training_monotony(tmpdb, today.isoformat())
    assert out["band"] == "rest"


# ── Z2 minutes ──

def test_z2_counts_only_in_zone(tmpdb):
    cfg = _make_cfg(tmpdb, user_age=30)  # HRmax=190, Z2=114-133
    today = date.today()
    _fill_activities(
        tmpdb, today, 7,
        calories=[300, 400, 500, 350, 0, 600, 0],
        duration_s=[3600, 1800, 5400, 2400, 0, 1800, 0],
        avg_hr=[120, 100, 130, 145, 0, 125, 0],  # 60min Z2, 90min Z2, 30min Z2
    )
    out = db.z2_minutes_for_week(tmpdb, today.isoformat(), cfg)
    assert out["lower_bpm"] == 114
    assert out["upper_bpm"] == 133
    assert out["minutes"] == 60 + 90 + 30


def test_z2_uses_explicit_hrmax_when_set(tmpdb):
    cfg = _make_cfg(tmpdb, user_age=30, user_hrmax=200)
    today = date.today()
    out = db.z2_minutes_for_week(tmpdb, today.isoformat(), cfg)
    assert out["hrmax"] == 200
    assert out["lower_bpm"] == 120
    assert out["upper_bpm"] == 140


# ── Sleep debt ──

def test_sleep_debt_accumulates_when_under_target(tmpdb):
    cfg = _make_cfg(tmpdb, sleep_target_hours=8)
    today = date.today()
    _fill_summaries(
        tmpdb, today, 7,
        sleep_seconds=[6 * 3600] * 7,  # 2h short × 7 days = 14h debt
    )
    out = db.sleep_debt(tmpdb, today.isoformat(), cfg)
    assert out["total_h"] == pytest.approx(14.0)


def test_sleep_debt_negative_with_oversleep(tmpdb):
    cfg = _make_cfg(tmpdb, sleep_target_hours=8)
    today = date.today()
    _fill_summaries(
        tmpdb, today, 7,
        sleep_seconds=[9 * 3600] * 7,  # 1h surplus × 7 days = -7h
    )
    out = db.sleep_debt(tmpdb, today.isoformat(), cfg)
    assert out["total_h"] == pytest.approx(-7.0)


def test_sleep_debt_skips_missing_days(tmpdb):
    cfg = _make_cfg(tmpdb, sleep_target_hours=8)
    today = date.today()
    out = db.sleep_debt(tmpdb, today.isoformat(), cfg)
    assert out["total_h"] == 0.0
    assert all(d["deficit_h"] is None for d in out["by_day"])


# ── Daily readiness history (heatmap) ──

def test_daily_readiness_history_returns_one_per_day(tmpdb):
    cfg = _make_cfg(tmpdb)
    out = db.daily_readiness_history(tmpdb, days=7, cfg=cfg)
    assert len(out) == 7
    # Production now anchors on Pi-local 'today' (db._local_today), so this
    # plain `date.today()` matches and is no longer UTC-flaky.
    today = date.today().isoformat()
    assert out[-1]["date"] == today
