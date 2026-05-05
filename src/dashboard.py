"""Tiny FastAPI + HTMX dashboard.

Optional component — install fastapi + uvicorn separately. Run with:
    python -m src.cli dashboard --host 0.0.0.0 --port 8000

Serves a single page with Chart.js panels for: realtime HR (last 6 hours),
HRV samples (last 7 days), and the daily summary table (last 14 days).
HTMX is used for the table refresh; charts are vanilla fetch + Chart.js.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from . import db
from .config import Config

if TYPE_CHECKING:  # type-only — avoid hard dep at module load time
    from fastapi import FastAPI

log = logging.getLogger(__name__)


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>garmin-monitor</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <style>
    body { font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 1100px; }
    h1 { margin-bottom: 0.5rem; }
    .row { display: grid; gap: 1rem; grid-template-columns: 1fr 1fr; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; }
    canvas { max-height: 280px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th, td { padding: 0.35rem 0.6rem; border-bottom: 1px solid #eee; text-align: right; }
    th:first-child, td:first-child { text-align: left; }
    th { background: #f6f6f6; }
    .meta { color: #666; font-size: 0.85rem; }
  </style>
</head>
<body>
  <h1>garmin-monitor</h1>
  <p class="meta">Auto-refreshes every 60s. SQLite at server-side; no data leaves the Pi.</p>

  <div class="row">
    <div class="card">
      <h3>HR — last 6h</h3>
      <canvas id="hrChart"></canvas>
    </div>
    <div class="card">
      <h3>HRV (RMSSD) — last 7d</h3>
      <canvas id="hrvChart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-top:1rem;">
    <h3>Daily summary — last 14d</h3>
    <div hx-get="/partials/daily" hx-trigger="load, every 60s" hx-swap="innerHTML">
      Loading...
    </div>
  </div>

  <div class="card" style="margin-top:1rem;">
    <h3>Activities — last 7d</h3>
    <div hx-get="/partials/activities" hx-trigger="load, every 60s" hx-swap="innerHTML">
      Loading...
    </div>
  </div>

  <script>
    async function loadChart(elId, url, label) {
      const data = await fetch(url).then(r => r.json());
      const ctx = document.getElementById(elId);
      new Chart(ctx, {
        type: 'line',
        data: {
          labels: data.map(p => p.t),
          datasets: [{ label, data: data.map(p => p.v), borderWidth: 1, pointRadius: 1 }]
        },
        options: { animation: false, scales: { x: { ticks: { maxTicksLimit: 6 } } } }
      });
    }
    loadChart('hrChart', '/api/hr', 'bpm');
    loadChart('hrvChart', '/api/hrv', 'ms');
  </script>
</body>
</html>
"""


def _hr_recent(db_path, hours: int = 6) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, bpm FROM hr_realtime WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    return [{"t": r[0], "v": r[1]} for r in rows]


def _hrv_recent(db_path, days: int = 7) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, rmssd_ms FROM hrv WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    return [{"t": r[0], "v": r[1]} for r in rows]


def _daily_recent(db_path, days: int = 14) -> list[dict]:
    with db.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, resting_hr, max_hr, steps, sleep_seconds, "
            "stress_avg, hrv_overnight FROM daily_summary "
            "ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return [dict(r) for r in rows]


def _activities_table_html(rows: list[dict]) -> str:
    if not rows:
        return "<p class='meta'>No activities in the last 7 days.</p>"

    def fmt_dur(s):
        if not s:
            return "—"
        m, sec = divmod(int(s), 60)
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"

    def fmt_dist(d):
        return f"{d / 1000:.2f} km" if d else "—"

    head = (
        "<thead><tr><th>Date</th><th>Type</th><th>Name</th>"
        "<th>Duration</th><th>Distance</th><th>Avg HR</th>"
        "<th>Calories</th></tr></thead>"
    )
    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr>"
            f"<td>{r['date']}</td>"
            f"<td>{r.get('activity_type') or '—'}</td>"
            f"<td>{r.get('name') or '—'}</td>"
            f"<td>{fmt_dur(r.get('duration_s'))}</td>"
            f"<td>{fmt_dist(r.get('distance_m'))}</td>"
            f"<td>{r.get('avg_hr') or '—'}</td>"
            f"<td>{r.get('calories') or '—'}</td>"
            "</tr>"
        )
    return f"<table>{head}<tbody>{''.join(body_rows)}</tbody></table>"


def _daily_table_html(rows: list[dict]) -> str:
    if not rows:
        return "<p class='meta'>No daily summaries yet — run <code>python -m src.cli poll</code>.</p>"

    def fmt_sleep(s):
        if not s:
            return "—"
        h, rem = divmod(int(s), 3600)
        return f"{h}h{rem // 60:02d}m"

    head = (
        "<thead><tr><th>Date</th><th>RHR</th><th>Max HR</th><th>Steps</th>"
        "<th>Sleep</th><th>Stress</th><th>HRV</th></tr></thead>"
    )
    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr>"
            f"<td>{r['date']}</td>"
            f"<td>{r.get('resting_hr') or '—'}</td>"
            f"<td>{r.get('max_hr') or '—'}</td>"
            f"<td>{r.get('steps') or '—'}</td>"
            f"<td>{fmt_sleep(r.get('sleep_seconds'))}</td>"
            f"<td>{r.get('stress_avg') or '—'}</td>"
            f"<td>{r.get('hrv_overnight') or '—'}</td>"
            "</tr>"
        )
    return f"<table>{head}<tbody>{''.join(body_rows)}</tbody></table>"


def create_app(cfg: Config) -> "FastAPI":
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="garmin-monitor")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/hr")
    def api_hr() -> JSONResponse:
        return JSONResponse(_hr_recent(cfg.db_path))

    @app.get("/api/hrv")
    def api_hrv() -> JSONResponse:
        return JSONResponse(_hrv_recent(cfg.db_path))

    @app.get("/partials/daily", response_class=HTMLResponse)
    def partial_daily() -> str:
        return _daily_table_html(_daily_recent(cfg.db_path))

    @app.get("/partials/activities", response_class=HTMLResponse)
    def partial_activities() -> str:
        return _activities_table_html(db.recent_activities(cfg.db_path, days=7))

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the dashboard with uvicorn (blocking)."""
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level=cfg.log_level.lower())
