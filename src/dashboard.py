"""FastAPI + HTMX + Chart.js dashboard.

Single-page interactive view over the SQLite store. Run with:
    python -m src.cli dashboard --host 0.0.0.0 --port 8000

Frontend stack: vanilla JS + Chart.js + chartjs-plugin-zoom + Tailwind +
HTMX, all from CDN. No build step.
"""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db
from .config import Config

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

ACTIVITY_EMOJI = {
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
    "capoeira": "🥋",
    "martial_arts": "🥋",
}

ALERT_COLORS = {
    "illness_risk": "bg-red-900/50 border-red-700 text-red-200",
    "training_ready": "bg-emerald-900/50 border-emerald-700 text-emerald-200",
    "recovery_day": "bg-amber-900/50 border-amber-700 text-amber-200",
    "hrv_drop": "bg-amber-900/50 border-amber-700 text-amber-200",
    "resting_hr_trend": "bg-amber-900/50 border-amber-700 text-amber-200",
    "resting_hr_high": "bg-amber-900/50 border-amber-700 text-amber-200",
    "hr_resting_window": "bg-orange-900/50 border-orange-700 text-orange-200",
}

ROLLING_WINDOW = 7

INDEX_HTML = """<!doctype html>
<html lang=en>
<head>
  <meta charset=utf-8>
  <meta name=viewport content="width=device-width,initial-scale=1">
  <title>🌙 Garmin Monitor</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <style>
    body { background: #0b1020; color: #e5e7eb; font-family: ui-sans-serif, system-ui, sans-serif; }
    .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 1rem; }
    .stat-num { font-size: 1.85rem; font-weight: 700; line-height: 1.1; }
    .stat-label { color: #9ca3af; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.7rem; font-weight: 600; }
    canvas { max-height: 320px; }
  </style>
</head>
<body class="min-h-screen">
  <header class="px-6 py-4 border-b border-gray-800 flex items-center justify-between">
    <div>
      <h1 class="text-xl font-bold">🌙 Garmin Monitor</h1>
      <p class="text-xs text-gray-400">Pi: __HOSTNAME__ · last refresh
        <span id="last-refresh" class="text-gray-300">—</span></p>
    </div>
    <button onclick="window.location.reload()"
            class="px-3 py-1.5 text-xs rounded bg-gray-800 hover:bg-gray-700 border border-gray-700">
      Refresh now
    </button>
  </header>

  <main class="p-4 max-w-7xl mx-auto space-y-4"
        hx-trigger="load, every 5m" hx-get="/partials/all" hx-target="#root">
    <div id="root">Loading…</div>
  </main>

  <script>
    // Re-render charts whenever HTMX swaps a partial that contains JSON-data scripts.
    document.body.addEventListener('htmx:afterSwap', () => {
      document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
      renderAll();
    });

    function $(id) { return document.getElementById(id); }
    function readJSON(id) { const el = $(id); return el ? JSON.parse(el.textContent) : null; }

    let charts = {};
    function destroyChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

    function rolling(values, window) {
      const out = [];
      for (let i = 0; i < values.length; i++) {
        const slice = values.slice(Math.max(0, i - window + 1), i + 1).filter(v => v != null);
        out.push(slice.length ? slice.reduce((a, b) => a + b, 0) / slice.length : null);
      }
      return out;
    }

    function renderAll() {
      renderHR();
      renderTrend('hrvTrendChart', readJSON('hrvTrendData'), 'HRV (ms)', '#34d399');
      renderTrend('rhrTrendChart', readJSON('rhrTrendData'), 'Resting HR (bpm)', '#f472b6');
      renderSleepStages();
      renderSleepDuration();
      renderTrainingLoad();
      renderScatter();
      renderBodyBatterySpark();
    }

    function renderHR() {
      const data = readJSON('hrRealtimeData') || [];
      destroyChart('hrChart');
      const ctx = $('hrChart');
      if (!ctx) return;
      charts.hrChart = new Chart(ctx, {
        type: 'line',
        data: {
          datasets: [{
            label: 'HR (bpm)',
            data: data.map(p => ({ x: p.t, y: p.v })),
            borderColor: '#f87171',
            backgroundColor: 'rgba(248,113,113,0.15)',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.2,
          }],
        },
        options: {
          animation: false,
          parsing: false,
          scales: {
            x: { type: 'time', ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
            y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
          },
          plugins: {
            legend: { labels: { color: '#e5e7eb' } },
            zoom: {
              pan: { enabled: true, mode: 'x' },
              zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },
            },
          },
        },
      });
    }

    function renderTrend(canvasId, payload, label, color) {
      destroyChart(canvasId);
      const ctx = $(canvasId);
      if (!ctx || !payload) return;
      const labels = payload.map(r => r.date);
      const values = payload.map(r => r.value);
      const avg = rolling(values, 7);
      charts[canvasId] = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label, data: values, borderColor: color, backgroundColor: color + '33',
              borderWidth: 1.5, pointRadius: 2, tension: 0.2 },
            { label: '7-day avg', data: avg, borderColor: color, borderDash: [4, 4],
              borderWidth: 1, pointRadius: 0, tension: 0.2 },
          ],
        },
        options: {
          animation: false,
          scales: {
            x: { ticks: { color: '#9ca3af', maxTicksLimit: 8 }, grid: { color: '#1f2937' } },
            y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
          },
          plugins: { legend: { labels: { color: '#e5e7eb' } } },
        },
      });
    }

    function renderSleepStages() {
      const data = readJSON('sleepStagesData');
      destroyChart('sleepStagesChart');
      const ctx = $('sleepStagesChart');
      if (!ctx || !data) return;
      charts.sleepStagesChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: ['Deep', 'Light', 'REM', 'Awake'],
          datasets: [{
            data: [data.deep, data.light, data.rem, data.awake],
            backgroundColor: ['#6366f1', '#60a5fa', '#a78bfa', '#f59e0b'],
            borderColor: '#0b1020',
          }],
        },
        options: { animation: false, plugins: { legend: { labels: { color: '#e5e7eb' } } } },
      });
    }

    function renderSleepDuration() {
      const data = readJSON('sleepDurationData') || [];
      destroyChart('sleepDurationChart');
      const ctx = $('sleepDurationChart');
      if (!ctx) return;
      charts.sleepDurationChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.map(r => r.date),
          datasets: [{
            label: 'Sleep (hours)',
            data: data.map(r => r.hours),
            backgroundColor: '#60a5fa',
          }],
        },
        options: {
          animation: false,
          scales: {
            x: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
            y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
          },
          plugins: { legend: { labels: { color: '#e5e7eb' } } },
        },
      });
    }

    function renderTrainingLoad() {
      const data = readJSON('trainingLoadData') || [];
      destroyChart('trainingLoadChart');
      const ctx = $('trainingLoadChart');
      if (!ctx) return;
      charts.trainingLoadChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.map(r => r.date),
          datasets: [{
            label: 'Calories',
            data: data.map(r => r.calories),
            backgroundColor: '#fb923c',
          }],
        },
        options: {
          animation: false,
          scales: {
            x: { ticks: { color: '#9ca3af', maxTicksLimit: 10 }, grid: { color: '#1f2937' } },
            y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
          },
          plugins: { legend: { labels: { color: '#e5e7eb' } } },
        },
      });
    }

    function renderScatter() {
      const data = readJSON('scatterData') || [];
      destroyChart('scatterChart');
      const ctx = $('scatterChart');
      if (!ctx) return;
      charts.scatterChart = new Chart(ctx, {
        type: 'scatter',
        data: {
          datasets: [{
            label: 'Day (HRV vs RHR)',
            data: data.map(r => ({ x: r.hrv, y: r.rhr })),
            backgroundColor: '#34d399',
          }],
        },
        options: {
          animation: false,
          scales: {
            x: { title: { display: true, text: 'HRV (ms)', color: '#9ca3af' },
                 ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
            y: { title: { display: true, text: 'Resting HR (bpm)', color: '#9ca3af' },
                 ticks: { color: '#9ca3af' }, grid: { color: '#1f2937' } },
          },
          plugins: { legend: { labels: { color: '#e5e7eb' } } },
        },
      });
    }

    function renderBodyBatterySpark() {
      const data = readJSON('bodyBatterySparkData') || [];
      destroyChart('bbSparkChart');
      const ctx = $('bbSparkChart');
      if (!ctx) return;
      charts.bbSparkChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: data.map(r => r.date),
          datasets: [{
            data: data.map(r => r.value),
            borderColor: '#a78bfa',
            backgroundColor: 'rgba(167,139,250,0.2)',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.4,
          }],
        },
        options: {
          animation: false,
          plugins: { legend: { display: false }, tooltip: { enabled: false } },
          scales: { x: { display: false }, y: { display: false } },
        },
      });
    }
  </script>
</body>
</html>
"""


def _coerce_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _today_iso() -> str:
    return date.today().isoformat()


def _yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _arrow(today_v: float | None, yest_v: float | None) -> str:
    if today_v is None or yest_v is None:
        return "→"
    if today_v > yest_v:
        return "▲"
    if today_v < yest_v:
        return "▼"
    return "→"


def _sleep_quality(seconds: int | None) -> tuple[str, str]:
    if not seconds:
        return ("—", "bg-gray-700 text-gray-300")
    h = seconds / 3600
    if h >= 7:
        return ("GOOD", "bg-emerald-900/60 text-emerald-300")
    if h >= 5:
        return ("OK", "bg-amber-900/60 text-amber-300")
    return ("POOR", "bg-red-900/60 text-red-300")


def _stress_color(avg: int | None) -> str:
    if avg is None:
        return "bg-gray-700 text-gray-300"
    if avg < 30:
        return "bg-emerald-900/60 text-emerald-300"
    if avg < 60:
        return "bg-amber-900/60 text-amber-300"
    return "bg-red-900/60 text-red-300"


def _sleep_stages_from_raw(raw_json: str | None) -> dict | None:
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    sleep = (data or {}).get("sleep") or {}
    dto = sleep.get("dailySleepDTO") or sleep
    deep = dto.get("deepSleepSeconds")
    light = dto.get("lightSleepSeconds")
    rem = dto.get("remSleepSeconds")
    awake = dto.get("awakeSleepSeconds")
    if all(v is None for v in (deep, light, rem, awake)):
        return None
    return {
        "deep": int(deep or 0),
        "light": int(light or 0),
        "rem": int(rem or 0),
        "awake": int(awake or 0),
    }


def get_today_summary(db_path: Path) -> dict:
    today = db.daily_summary_for(db_path, _today_iso()) or {}
    yest = db.daily_summary_for(db_path, _yesterday_iso()) or {}

    rhr_today = _coerce_float(today.get("resting_hr"))
    hrv_today = _coerce_float(today.get("hrv_overnight"))

    baseline_rows = db.recent_daily_metrics(db_path, ROLLING_WINDOW + 1)
    baseline = baseline_rows[:-1] if len(baseline_rows) > 1 else []
    hrv_vals = [r["hrv_overnight"] for r in baseline if r.get("hrv_overnight") is not None]
    hrv_baseline = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None

    if hrv_today is not None and hrv_baseline:
        delta = (hrv_today - hrv_baseline) / hrv_baseline
        if delta >= 0.05:
            hrv_state = "HIGH"
        elif delta <= -0.05:
            hrv_state = "LOW"
        else:
            hrv_state = "BALANCED"
    else:
        hrv_state = "—"

    return {
        "date": today.get("date") or _today_iso(),
        "resting_hr": today.get("resting_hr"),
        "resting_hr_arrow": _arrow(rhr_today, _coerce_float(yest.get("resting_hr"))),
        "hrv": today.get("hrv_overnight"),
        "hrv_arrow": _arrow(hrv_today, _coerce_float(yest.get("hrv_overnight"))),
        "hrv_state": hrv_state,
        "steps": today.get("steps") or 0,
        "sleep_seconds": today.get("sleep_seconds"),
        "body_battery": today.get("body_battery"),
        "stress_avg": today.get("stress_avg"),
    }


def get_hr_24h(db_path: Path, bucket_seconds: int = 120) -> list[dict]:
    """Return last 24h of HR averaged into `bucket_seconds`-wide buckets.

    The BLE listener writes ~1Hz, so 24h ≈ 86k rows — too many to plot. Bucket
    averaging keeps the chart responsive while preserving the shape.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    bucket = max(1, bucket_seconds)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT
                strftime('%Y-%m-%dT%H:%M:%SZ',
                    (CAST(strftime('%s', ts) AS INTEGER) / {bucket}) * {bucket},
                    'unixepoch') AS bucket_ts,
                AVG(bpm) AS avg_bpm
            FROM hr_realtime
            WHERE ts >= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts ASC
            """,
            (cutoff,),
        ).fetchall()
    return [{"t": r[0], "v": round(r[1], 1)} for r in rows]


def get_trends(db_path: Path, days: int) -> list[dict]:
    with db.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, resting_hr, hrv_overnight, sleep_seconds, body_battery "
            "FROM daily_summary ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_activities(db_path: Path, days: int) -> list[dict]:
    return db.recent_activities(db_path, days=days)


def get_alerts(db_path: Path, limit: int) -> list[dict]:
    return db.recent_alerts(db_path, limit=limit)


def get_readiness(db_path: Path) -> dict:
    rows = db.recent_daily_metrics(db_path, ROLLING_WINDOW + 1)
    if len(rows) < 2:
        return {
            "recommendation": "MAINTAIN",
            "label": "✅ MAINTAIN",
            "color": "bg-blue-900/40 text-blue-200 border-blue-700",
            "hrv_delta": None,
            "rhr_delta": None,
            "illness_risk": "green",
        }
    *baseline, today = rows
    hrv_vals = [r["hrv_overnight"] for r in baseline if r.get("hrv_overnight") is not None]
    rhr_vals = [r["resting_hr"] for r in baseline if r.get("resting_hr") is not None]
    hrv_baseline = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None
    rhr_baseline = sum(rhr_vals) / len(rhr_vals) if rhr_vals else None

    today_hrv = today.get("hrv_overnight")
    today_rhr = today.get("resting_hr")
    hrv_delta = (today_hrv - hrv_baseline) / hrv_baseline if today_hrv and hrv_baseline else None
    rhr_delta = (today_rhr - rhr_baseline) / rhr_baseline if today_rhr and rhr_baseline else None

    if hrv_delta is None:
        rec, label, color = "MAINTAIN", "✅ MAINTAIN", "bg-blue-900/40 text-blue-200 border-blue-700"
    elif hrv_delta >= 0.10:
        rec, label, color = "TRAIN HARD", "💪 TRAIN HARD", "bg-emerald-900/40 text-emerald-200 border-emerald-700"
    elif hrv_delta <= -0.10:
        rec, label, color = "RECOVER", "🔄 RECOVER", "bg-amber-900/40 text-amber-200 border-amber-700"
    else:
        rec, label, color = "MAINTAIN", "✅ MAINTAIN", "bg-blue-900/40 text-blue-200 border-blue-700"

    rhr_up = rhr_delta is not None and rhr_delta > 0.05
    hrv_down = hrv_delta is not None and hrv_delta < -0.10
    if rhr_up and hrv_down:
        risk = "red"
    elif rhr_up or hrv_down:
        risk = "yellow"
    else:
        risk = "green"

    return {
        "recommendation": rec,
        "label": label,
        "color": color,
        "hrv_delta": hrv_delta,
        "rhr_delta": rhr_delta,
        "illness_risk": risk,
    }


def _fmt_seconds(s: int | None) -> str:
    if not s:
        return "—"
    h, rem = divmod(int(s), 3600)
    return f"{h}h{rem // 60:02d}m"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.0f}%"


def _stat_card(label: str, value: str, sub: str = "", extra: str = "") -> str:
    return (
        f'<div class="card"><div class="stat-label">{label}</div>'
        f'<div class="stat-num">{value}</div>'
        f'<div class="text-xs text-gray-400">{sub}</div>'
        f'{extra}</div>'
    )


def _row1_html(summary: dict, body_battery_spark: list[dict]) -> str:
    rhr = summary.get("resting_hr") or "—"
    hrv = summary.get("hrv") or "—"
    steps = summary.get("steps") or 0
    bb = summary.get("body_battery")
    stress = summary.get("stress_avg")
    sleep_label, sleep_class = _sleep_quality(summary.get("sleep_seconds"))

    steps_pct = min(100, int(steps / 100)) if steps else 0
    bar = (
        f'<div class="mt-2 h-1.5 bg-gray-800 rounded">'
        f'<div class="h-1.5 rounded bg-emerald-500" style="width:{steps_pct}%"></div></div>'
    )

    hrv_state = summary.get("hrv_state", "—")
    hrv_state_color = {
        "HIGH": "bg-emerald-900/60 text-emerald-300",
        "BALANCED": "bg-blue-900/60 text-blue-300",
        "LOW": "bg-red-900/60 text-red-300",
    }.get(hrv_state, "bg-gray-700 text-gray-300")

    bb_value = f"{bb}" if bb is not None else "—"
    bb_extra = (
        f'<div class="mt-2"><script type="application/json" id="bodyBatterySparkData">'
        f'{json.dumps(body_battery_spark)}</script>'
        f'<canvas id="bbSparkChart" height="40"></canvas></div>'
    )

    return f"""
    <section class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {_stat_card("Resting HR", f"{rhr} <span class='text-base text-gray-400'>{summary['resting_hr_arrow']}</span>", "bpm vs yesterday")}
      {_stat_card("HRV", f"{hrv} <span class='text-base text-gray-400'>{summary['hrv_arrow']}</span>",
                  "ms (overnight)",
                  f'<span class="badge mt-2 {hrv_state_color}">{hrv_state}</span>')}
      {_stat_card("Steps", f"{steps:,}", "goal 10,000", bar)}
      {_stat_card("Sleep", _fmt_seconds(summary.get('sleep_seconds')), "last night",
                  f'<span class="badge mt-2 {sleep_class}">{sleep_label}</span>')}
      {_stat_card("Body Battery", bb_value, "current", bb_extra)}
      {_stat_card("Stress", f"{stress if stress is not None else '—'}", "avg today",
                  f'<span class="badge mt-2 {_stress_color(stress)}">·</span>')}
    </section>
    """


def _row2_html(hr_data: list[dict]) -> str:
    return f"""
    <section class="card">
      <h2 class="font-semibold mb-2">HR — last 24h</h2>
      <p class="text-xs text-gray-400 mb-2">Scroll to zoom · drag to pan</p>
      <script type="application/json" id="hrRealtimeData">{json.dumps(hr_data)}</script>
      <canvas id="hrChart"></canvas>
    </section>
    """


def _row3_html(trends: list[dict]) -> str:
    hrv_data = [{"date": r["date"], "value": r["hrv_overnight"]} for r in trends]
    rhr_data = [{"date": r["date"], "value": r["resting_hr"]} for r in trends]
    return f"""
    <section class="grid md:grid-cols-2 gap-3">
      <div class="card">
        <h2 class="font-semibold mb-2">HRV — last 30d</h2>
        <script type="application/json" id="hrvTrendData">{json.dumps(hrv_data)}</script>
        <canvas id="hrvTrendChart"></canvas>
      </div>
      <div class="card">
        <h2 class="font-semibold mb-2">Resting HR — last 30d</h2>
        <script type="application/json" id="rhrTrendData">{json.dumps(rhr_data)}</script>
        <canvas id="rhrTrendChart"></canvas>
      </div>
    </section>
    """


def _row4_html(stages: dict | None, sleep_dur: list[dict]) -> str:
    stages_block = (
        f'<script type="application/json" id="sleepStagesData">{json.dumps(stages)}</script>'
        f'<canvas id="sleepStagesChart"></canvas>'
        if stages
        else '<p class="text-gray-500 text-sm">No sleep-stage data for last night.</p>'
    )
    return f"""
    <section class="grid md:grid-cols-2 gap-3">
      <div class="card">
        <h2 class="font-semibold mb-2">Sleep stages — last night</h2>
        {stages_block}
      </div>
      <div class="card">
        <h2 class="font-semibold mb-2">Sleep duration — last 14d</h2>
        <script type="application/json" id="sleepDurationData">{json.dumps(sleep_dur)}</script>
        <canvas id="sleepDurationChart"></canvas>
      </div>
    </section>
    """


def _training_effect_badge(te: float | None) -> str:
    if te is None:
        return '<span class="badge bg-gray-700 text-gray-300">—</span>'
    color = "bg-gray-700 text-gray-300"
    if te >= 4:
        color = "bg-red-900/60 text-red-300"
    elif te >= 3:
        color = "bg-amber-900/60 text-amber-300"
    elif te >= 2:
        color = "bg-emerald-900/60 text-emerald-300"
    elif te >= 1:
        color = "bg-blue-900/60 text-blue-300"
    return f'<span class="badge {color}">{te:.1f}</span>'


def _activity_emoji(activity_type: str | None, name: str | None) -> str:
    name_lc = (name or "").lower()
    if "capoeira" in name_lc:
        return "🥋"
    return ACTIVITY_EMOJI.get(activity_type or "", "💪")


def _row5_html(activities: list[dict], cal_load: list[dict]) -> str:
    if not activities:
        rows_html = '<tr><td colspan="6" class="text-gray-500 text-sm py-2">No activities in the last 14 days.</td></tr>'
    else:
        rows = []
        for a in activities:
            emoji = _activity_emoji(a.get("activity_type"), a.get("name"))
            mins = (a.get("duration_s") or 0) // 60
            avg = a.get("avg_hr") or "—"
            cals = a.get("calories") or "—"
            rows.append(
                "<tr class='border-t border-gray-800'>"
                f"<td class='py-1.5 pr-3 text-gray-400'>{a.get('date') or '—'}</td>"
                f"<td class='py-1.5 pr-3 text-lg'>{emoji}</td>"
                f"<td class='py-1.5 pr-3'>{a.get('name') or (a.get('activity_type') or '—')}</td>"
                f"<td class='py-1.5 pr-3 text-right'>{mins}m</td>"
                f"<td class='py-1.5 pr-3 text-right'>{avg}</td>"
                f"<td class='py-1.5 pr-3 text-right'>{cals}</td>"
                f"<td class='py-1.5 pr-3 text-right'>{_training_effect_badge(a.get('training_effect'))}</td>"
                "</tr>"
            )
        rows_html = "".join(rows)
    return f"""
    <section class="grid lg:grid-cols-2 gap-3">
      <div class="card overflow-x-auto">
        <h2 class="font-semibold mb-2">Activities — last 14d</h2>
        <table class="w-full text-sm">
          <thead><tr class="text-gray-400 text-xs uppercase">
            <th class="text-left pr-3">Date</th>
            <th class="pr-3">Type</th>
            <th class="text-left pr-3">Name</th>
            <th class="pr-3 text-right">Dur</th>
            <th class="pr-3 text-right">Avg HR</th>
            <th class="pr-3 text-right">kcal</th>
            <th class="pr-3 text-right">TE</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
      <div class="card">
        <h2 class="font-semibold mb-2">Training load — last 30d (kcal)</h2>
        <script type="application/json" id="trainingLoadData">{json.dumps(cal_load)}</script>
        <canvas id="trainingLoadChart"></canvas>
      </div>
    </section>
    """


def _row6_html(readiness: dict, scatter: list[dict]) -> str:
    risk_color = {
        "green": "bg-emerald-500",
        "yellow": "bg-amber-500",
        "red": "bg-red-500",
    }[readiness["illness_risk"]]
    risk_text = {
        "green": "Healthy — no warning signs",
        "yellow": "Watch — one of RHR/HRV is off",
        "red": "Elevated risk — both RHR & HRV are off",
    }[readiness["illness_risk"]]
    return f"""
    <section class="grid md:grid-cols-3 gap-3">
      <div class="card border-2 {readiness['color']}">
        <div class="stat-label">Today's recommendation</div>
        <div class="text-2xl font-bold mt-1">{readiness['label']}</div>
        <div class="text-xs text-gray-400 mt-2">
          HRV vs baseline: {_fmt_pct(readiness['hrv_delta'])} ·
          RHR vs baseline: {_fmt_pct(readiness['rhr_delta'])}
        </div>
      </div>
      <div class="card">
        <h2 class="font-semibold mb-2">HRV vs RHR — last 14d</h2>
        <script type="application/json" id="scatterData">{json.dumps(scatter)}</script>
        <canvas id="scatterChart"></canvas>
      </div>
      <div class="card">
        <h2 class="font-semibold mb-2">Illness risk</h2>
        <div class="flex items-center gap-3 mt-3">
          <span class="inline-block w-5 h-5 rounded-full {risk_color}"></span>
          <span class="text-sm">{risk_text}</span>
        </div>
      </div>
    </section>
    """


def _row7_html(alerts_list: list[dict]) -> str:
    if not alerts_list:
        body = '<p class="text-gray-500 text-sm">No alerts yet.</p>'
    else:
        items = []
        for a in alerts_list:
            color = ALERT_COLORS.get(a.get("kind", ""), "bg-gray-800 border-gray-700 text-gray-200")
            msg = a.get("message") or ""
            if not msg:
                # Older rows stored only payload — show kind + payload as fallback.
                msg = f"<span class='font-mono text-xs'>{a.get('payload') or ''}</span>"
            items.append(
                f'<li class="border-l-4 px-3 py-2 rounded {color}">'
                f'<div class="text-xs text-gray-400">{a.get("ts")} · '
                f'<span class="font-mono">{a.get("kind")}</span></div>'
                f'<div class="text-sm mt-0.5">{msg}</div>'
                f'</li>'
            )
        body = f'<ul class="space-y-2">{"".join(items)}</ul>'
    return f"""
    <section class="card">
      <h2 class="font-semibold mb-2">Recent alerts</h2>
      {body}
    </section>
    """


def render_full(cfg: Config) -> str:
    summary = get_today_summary(cfg.db_path)
    hr = get_hr_24h(cfg.db_path)
    trends = get_trends(cfg.db_path, 30)
    sleep_dur = [
        {"date": r["date"], "hours": round((r["sleep_seconds"] or 0) / 3600, 2)}
        for r in trends[-14:]
    ]
    today_row = db.daily_summary_for(cfg.db_path, _today_iso()) or {}
    yest_row = db.daily_summary_for(cfg.db_path, _yesterday_iso()) or {}
    stages = _sleep_stages_from_raw(today_row.get("raw_json")) or _sleep_stages_from_raw(
        yest_row.get("raw_json")
    )
    bb_spark = [{"date": r["date"], "value": r["body_battery"]} for r in trends[-14:]]

    activities = get_activities(cfg.db_path, 14)
    cal_load = []
    by_date: dict[str, int] = {}
    for a in activities:
        d = a.get("date")
        if not d:
            continue
        by_date[d] = by_date.get(d, 0) + (a.get("calories") or 0)
    for r in trends:
        cal_load.append({"date": r["date"], "calories": by_date.get(r["date"], 0)})

    readiness = get_readiness(cfg.db_path)
    scatter = [
        {"hrv": r["hrv_overnight"], "rhr": r["resting_hr"]}
        for r in trends[-14:]
        if r["hrv_overnight"] is not None and r["resting_hr"] is not None
    ]
    alerts_list = get_alerts(cfg.db_path, 10)

    return "\n".join(
        [
            _row1_html(summary, bb_spark),
            _row2_html(hr),
            _row3_html(trends),
            _row4_html(stages, sleep_dur),
            _row5_html(activities, cal_load),
            _row6_html(readiness, scatter),
            _row7_html(alerts_list),
        ]
    )


def create_app(cfg: Config) -> "FastAPI":
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="garmin-monitor")
    hostname = socket.gethostname()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.replace("__HOSTNAME__", hostname)

    @app.get("/partials/all", response_class=HTMLResponse)
    def partials_all() -> str:
        return render_full(cfg)

    @app.get("/api/summary")
    def api_summary() -> JSONResponse:
        return JSONResponse(get_today_summary(cfg.db_path))

    @app.get("/api/hr-realtime")
    def api_hr() -> JSONResponse:
        return JSONResponse(get_hr_24h(cfg.db_path))

    @app.get("/api/trends")
    def api_trends(days: int = 30) -> JSONResponse:
        return JSONResponse(get_trends(cfg.db_path, days))

    @app.get("/api/activities")
    def api_activities(days: int = 14) -> JSONResponse:
        return JSONResponse(get_activities(cfg.db_path, days))

    @app.get("/api/alerts")
    def api_alerts(limit: int = 10) -> JSONResponse:
        return JSONResponse(get_alerts(cfg.db_path, limit))

    @app.get("/api/readiness")
    def api_readiness() -> JSONResponse:
        return JSONResponse(get_readiness(cfg.db_path))

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the dashboard with uvicorn (blocking)."""
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level=cfg.log_level.lower())
