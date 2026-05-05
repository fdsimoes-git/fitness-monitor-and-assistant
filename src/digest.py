"""Daily digest: a one-shot Telegram message summarizing yesterday's metrics.

Run from a systemd timer at e.g. 08:00 local time. Reads yesterday's row
from `daily_summary` and posts a short Markdown message via the Telegram bot.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from . import db
from .alerts import send_telegram
from .config import Config

log = logging.getLogger(__name__)


def _format_seconds(seconds: int | None) -> str:
    if not seconds:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


_ACTIVITY_EMOJI = {
    "running": "🏃",
    "treadmill_running": "🏃",
    "trail_running": "🏃",
    "cycling": "🚴",
    "road_biking": "🚴",
    "mountain_biking": "🚴",
    "indoor_cycling": "🚴",
    "swimming": "🏊",
    "lap_swimming": "🏊",
    "open_water_swimming": "🏊",
    "walking": "🚶",
    "hiking": "🥾",
    "strength_training": "🏋️",
    "yoga": "🧘",
}


def _format_activity_line(a: dict) -> str:
    atype = a.get("activity_type") or "activity"
    emoji = _ACTIVITY_EMOJI.get(atype, "💪")
    label = atype.replace("_", " ").title()
    mins = (a.get("duration_s") or 0) // 60
    avg = a.get("avg_hr")
    parts = [f"{emoji} {label} {mins}min"]
    if avg:
        parts.append(f"avg {avg}bpm")
    return ", ".join(parts)


def build_digest(cfg: Config, target_date: date | None = None) -> str | None:
    """Build the digest text. Returns None if no data exists for the target date."""
    target_date = target_date or (date.today() - timedelta(days=1))
    summary = db.daily_summary_for(cfg.db_path, target_date.isoformat())
    activities = db.activities_for_date(cfg.db_path, target_date.isoformat())
    meals = db.meals_for_date(cfg.db_path, target_date.isoformat())

    if not summary and not activities and not meals:
        return None

    s = summary or {}
    lines = [
        f"☀️ *Daily digest* — {target_date.isoformat()}",
        "",
        f"• Resting HR: *{s.get('resting_hr') or '—'}* bpm",
        f"• Max HR: {s.get('max_hr') or '—'} bpm",
        f"• Avg HR: {s.get('avg_hr') or '—'} bpm",
        f"• Steps: {s.get('steps') or '—'}",
        f"• Sleep: {_format_seconds(s.get('sleep_seconds'))}",
        f"• Stress avg: {s.get('stress_avg') or '—'}",
        f"• Body battery: {s.get('body_battery') or '—'}",
        f"• HRV (overnight): {s.get('hrv_overnight') or '—'} ms",
    ]
    if activities:
        lines.append("")
        for a in activities:
            lines.append(_format_activity_line(a))

    balance = db.calorie_balance_for_date(cfg.db_path, target_date.isoformat(), cfg=cfg)
    if balance.get("meal_count", 0) > 0:
        bal_kcal = balance["balance_kcal"]
        sign = "surplus" if bal_kcal >= 0 else "deficit"
        lines.append("")
        lines.append(
            f"🍽️ *Nutrition Summary*"
        )
        lines.append(
            f"Intake: {balance['eaten_kcal']:.0f}kcal"
        )
        lines.append(
            f"\nExpenditure breakdown:"
        )
        lines.append(
            f"  • BMR (basal metabolic rate): {balance['bmr_kcal']}kcal"
        )
        lines.append(
            f"  • Steps: {balance['steps_burned_kcal']}kcal"
        )
        if balance['burned_kcal'] > 0:
            lines.append(
                f"  • Activities: {balance['burned_kcal']}kcal"
            )
        lines.append(
            f"  *Total burned: {balance['total_burned_kcal']}kcal*"
        )
        lines.append(
            f"\n*Balance: {bal_kcal:+.0f}kcal {sign}*"
        )
        lines.append(
            f"\n💪 Macros: {balance['protein_g']:.0f}g protein / "
            f"{balance['carbs_g']:.0f}g carbs / {balance['fat_g']:.0f}g fat"
        )
    return "\n".join(lines)


def send_digest(cfg: Config, target_date: date | None = None) -> bool:
    text = build_digest(cfg, target_date)
    if text is None:
        log.info("No data for digest target date; skipping send")
        return False
    sent = send_telegram(cfg, text)
    log.info("Digest sent=%s for %s", sent, target_date or "yesterday")
    return sent
