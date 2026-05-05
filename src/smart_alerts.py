"""Higher-level alert checks that run on each poll.

Independent checks driven by the daily_summary table the poller populates:

1. Resting-HR trend — resting_hr climbed strictly for 5 days in a row.
2. HRV drop — last night's HRV is >20% below the 7-day baseline.
3. Illness / overtraining — RHR ↑ AND HRV ↓ for 2+ consecutive days.
4. Training window — daily HRV vs baseline drives a "ready" / "recovery" tip.

All checks are pure-ish: they accept the data they need (or pull it from
db.py) and call alerts.maybe_alert() with cooldowns enforced by kind.
"""
from __future__ import annotations

import logging

from . import alerts, db
from .config import Config

log = logging.getLogger(__name__)

# Trend params
TREND_DAYS = 5

# HRV drop params
HRV_BASELINE_DAYS = 7
HRV_DROP_FRACTION = 0.20  # 20%
HRV_MIN_BASELINE_SAMPLES = 4  # avoid spurious alerts after a few days of data

# Illness / overtraining (Buchheit 2014, Plews 2013): RHR ↑ AND HRV ↓ on 2+ days.
ILLNESS_BASELINE_DAYS = 7
ILLNESS_RHR_RISE_FRACTION = 0.05  # 5%
ILLNESS_HRV_DROP_FRACTION = 0.10  # 10%
ILLNESS_CONSECUTIVE_DAYS = 2
ILLNESS_MIN_BASELINE_SAMPLES = 4

# Optimal training window (Kiviniemi 2007): HRV ±10% vs 7-day baseline.
TRAINING_BASELINE_DAYS = 7
TRAINING_HRV_FRACTION = 0.10
TRAINING_MIN_BASELINE_SAMPLES = 4

# Cooldown so the daily-readiness alerts fire at most once per day.
ONCE_PER_DAY_SECONDS = 22 * 3600


def _is_strictly_increasing(values: list[int]) -> bool:
    return len(values) >= 2 and all(b > a for a, b in zip(values, values[1:]))


def check_resting_hr_trend(cfg: Config) -> bool:
    """Strictly-increasing resting HR over the last TREND_DAYS days."""
    rows = db.recent_resting_hr(cfg.db_path, TREND_DAYS)
    if len(rows) < TREND_DAYS:
        return False
    values = [v for _, v in rows]
    if not _is_strictly_increasing(values):
        return False

    delta = values[-1] - values[0]
    text = (
        f"📊 Resting HR climbing *{TREND_DAYS}* days in a row: "
        f"{' → '.join(str(v) for v in values)} bpm (Δ +{delta})"
    )
    return alerts.maybe_alert(
        cfg,
        kind="resting_hr_trend",
        text=text,
        payload={"values": values, "dates": [d for d, _ in rows]},
    )


def check_hrv_drop(cfg: Config) -> bool:
    """Last night's HRV is >HRV_DROP_FRACTION below the prior-week mean."""
    rows = db.recent_hrv_overnight(cfg.db_path, HRV_BASELINE_DAYS + 1)
    if len(rows) < HRV_MIN_BASELINE_SAMPLES + 1:
        return False
    *baseline_rows, last = rows
    if len(baseline_rows) < HRV_MIN_BASELINE_SAMPLES:
        return False

    baseline = sum(v for _, v in baseline_rows) / len(baseline_rows)
    last_date, last_value = last
    if baseline <= 0:
        return False

    drop_fraction = (baseline - last_value) / baseline
    if drop_fraction < HRV_DROP_FRACTION:
        return False

    text = (
        f"💤 HRV down *{drop_fraction * 100:.0f}%* vs 7-day baseline: "
        f"{last_value} ms (avg {baseline:.0f} ms)"
    )
    return alerts.maybe_alert(
        cfg,
        kind="hrv_drop",
        text=text,
        payload={
            "last_date": last_date,
            "last_value": last_value,
            "baseline": baseline,
            "drop_fraction": drop_fraction,
        },
    )


def _baseline_mean(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if len(vals) < ILLNESS_MIN_BASELINE_SAMPLES:
        return None
    return sum(vals) / len(vals)


def check_illness_risk(cfg: Config) -> bool:
    """Two consecutive days of RHR ↑>5% AND HRV ↓>10% vs preceding 7-day mean."""
    needed = ILLNESS_BASELINE_DAYS + ILLNESS_CONSECUTIVE_DAYS
    rows = db.recent_daily_metrics(cfg.db_path, needed)
    if len(rows) < needed:
        return False

    streak: list[dict] = []
    for i in range(ILLNESS_CONSECUTIVE_DAYS):
        idx = len(rows) - 1 - i  # walk back from most-recent day
        baseline_rows = rows[idx - ILLNESS_BASELINE_DAYS:idx]
        rhr_baseline = _baseline_mean(baseline_rows, "resting_hr")
        hrv_baseline = _baseline_mean(baseline_rows, "hrv_overnight")
        if not rhr_baseline or not hrv_baseline:
            return False

        today_rhr = rows[idx].get("resting_hr")
        today_hrv = rows[idx].get("hrv_overnight")
        if today_rhr is None or today_hrv is None:
            return False

        rhr_rise = (today_rhr - rhr_baseline) / rhr_baseline
        hrv_drop = (hrv_baseline - today_hrv) / hrv_baseline
        if rhr_rise > ILLNESS_RHR_RISE_FRACTION and hrv_drop > ILLNESS_HRV_DROP_FRACTION:
            streak.append({
                "date": rows[idx]["date"],
                "rhr_rise": rhr_rise,
                "hrv_drop": hrv_drop,
            })
        else:
            return False  # streak broken

    if len(streak) < ILLNESS_CONSECUTIVE_DAYS:
        return False

    latest = streak[0]
    text = (
        f"⚠️ *Recovery alert*: your RHR is *{latest['rhr_rise'] * 100:.0f}%* "
        f"above baseline and HRV is *{latest['hrv_drop'] * 100:.0f}%* below baseline "
        f"for *{len(streak)}* days. Consider rest or check for illness."
    )
    return alerts.maybe_alert(
        cfg,
        kind="illness_risk",
        text=text,
        payload={"days": len(streak), "streak": streak},
        cooldown_seconds=ONCE_PER_DAY_SECONDS,
    )


def check_training_window(cfg: Config) -> bool:
    """Compare today's HRV to 7-day baseline; fire 'training_ready' or 'recovery_day'."""
    rows = db.recent_daily_metrics(cfg.db_path, TRAINING_BASELINE_DAYS + 1)
    if len(rows) < TRAINING_BASELINE_DAYS + 1:
        return False
    *baseline_rows, today_row = rows
    today_hrv = today_row.get("hrv_overnight")
    if today_hrv is None:
        return False
    hrv_baseline = _baseline_mean(baseline_rows, "hrv_overnight")
    if not hrv_baseline:
        return False

    delta = (today_hrv - hrv_baseline) / hrv_baseline
    payload = {
        "date": today_row["date"],
        "hrv": today_hrv,
        "baseline": hrv_baseline,
        "delta": delta,
    }

    if delta >= TRAINING_HRV_FRACTION:
        text = (
            f"💪 Great recovery today (HRV *+{delta * 100:.0f}%* vs baseline). "
            f"Good day for intense training."
        )
        return alerts.maybe_alert(
            cfg,
            kind="training_ready",
            text=text,
            payload=payload,
            cooldown_seconds=ONCE_PER_DAY_SECONDS,
        )
    if delta <= -TRAINING_HRV_FRACTION:
        text = (
            f"🔄 Low HRV today (*{delta * 100:.0f}%* vs baseline). "
            f"Prioritize recovery or light activity."
        )
        return alerts.maybe_alert(
            cfg,
            kind="recovery_day",
            text=text,
            payload=payload,
            cooldown_seconds=ONCE_PER_DAY_SECONDS,
        )
    return False


def run_smart_alerts(cfg: Config) -> None:
    """Run all checks in sequence. Failures in one don't block the others."""
    checks = (
        check_resting_hr_trend,
        check_hrv_drop,
        check_illness_risk,
        check_training_window,
    )
    for fn in checks:
        try:
            fn(cfg)
        except Exception as e:  # noqa: BLE001 — alerts must not break the poll
            log.exception("Smart alert %s failed: %s", fn.__name__, e)
