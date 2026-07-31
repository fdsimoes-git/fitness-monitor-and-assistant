"""FastAPI + HTMX + Chart.js dashboard.

Single-page interactive view over the SQLite store. Run with:
    python -m src.cli dashboard --host 0.0.0.0 --port 8000

Frontend stack: vanilla JS + Chart.js + chartjs-plugin-zoom + Tailwind +
HTMX, all from CDN. No build step.
"""
from __future__ import annotations

import json
import logging
import math
import socket
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db, metric_info
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
    "illness_risk": "border-magenta text-magenta-200 bg-magenta-glass",
    "training_ready": "border-lime text-lime-200 bg-lime-glass",
    "recovery_day": "border-amber text-amber-200 bg-amber-glass",
    "hrv_drop": "border-amber text-amber-200 bg-amber-glass",
    "resting_hr_trend": "border-amber text-amber-200 bg-amber-glass",
    "resting_hr_high": "border-amber text-amber-200 bg-amber-glass",
}

ROLLING_WINDOW = 7

# ACWR gauge geometry: upper semicircle of a single shared circle. Bands and
# needle must all use _gauge_point so their arcs stay on the same ring.
_GAUGE_CX, _GAUGE_CY, _GAUGE_R = 100.0, 100.0, 80.0
# Dial right end; needle_pct = ratio / _ACWR_GAUGE_MAX. Must stay above
# db.ACWR_CAUTION_MAX or the caution band would overrun the semicircle.
_ACWR_GAUGE_MAX = 2.0

# (lo, hi, stroke, opacity) per band, bounds in ACWR ratio units.
_ACWR_BANDS: tuple[tuple[float, float, str, str], ...] = (
    (0.0, db.ACWR_UNDERTRAINED_MAX, "#0e8aa3", "0.8"),
    (db.ACWR_UNDERTRAINED_MAX, db.ACWR_OPTIMAL_MAX, "#7ec831", "0.85"),
    (db.ACWR_OPTIMAL_MAX, db.ACWR_CAUTION_MAX, "#cc8e00", "0.85"),
    (db.ACWR_CAUTION_MAX, _ACWR_GAUGE_MAX, "#cc2570", "0.85"),
)


def _gauge_point(pct: float) -> tuple[float, float]:
    """Map pct in [0, 1] to the gauge's upper semicircle (0 → left end, 1 → right end)."""
    theta = math.pi * (1.0 - pct)
    return (
        _GAUGE_CX + _GAUGE_R * math.cos(theta),
        _GAUGE_CY - _GAUGE_R * math.sin(theta),
    )


def _gauge_arc_path(start_pct: float, end_pct: float) -> str:
    """SVG path `d` for an arc between two dial positions on the shared gauge circle."""
    x0, y0 = _gauge_point(start_pct)
    x1, y1 = _gauge_point(end_pct)
    return f"M {x0:.1f} {y0:.1f} A {_GAUGE_R:.0f} {_GAUGE_R:.0f} 0 0 1 {x1:.1f} {y1:.1f}"

# Heatmap colour scale (low → high readiness). Empty cells use gray.
_HEATMAP_PALETTE = ["#1a1f2e", "#2d1640", "#5a1f4d", "#a8264e", "#ff2e88", "#ffb300", "#adfa3c", "#00f5ff"]

INDEX_HTML = """<!doctype html>
<html lang=en>
<head>
  <meta charset=utf-8>
  <meta name=viewport content="width=device-width,initial-scale=1">
  <title>VITALS · Garmin Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/countup.js@2.8.0/dist/countUp.umd.js"></script>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <style>
    :root {
      --ink-0: #05060a;
      --ink-1: #0a0d18;
      --ink-2: #11162a;
      --ink-3: #1a2030;
      --line: #1f2740;
      --text: #e8edf2;
      --muted: #6a7282;

      --cyan: #00f5ff;
      --lime: #adfa3c;
      --amber: #ffb300;
      --magenta: #ff2e88;
      --violet: #a78bfa;
    }

    * { box-sizing: border-box; }

    html, body {
      background: var(--ink-0);
      color: var(--text);
      font-family: 'Inter', ui-sans-serif, system-ui, sans-serif;
      font-feature-settings: 'cv11', 'ss01', 'ss03';
      -webkit-font-smoothing: antialiased;
    }

    body::before {
      content: '';
      position: fixed; inset: 0;
      background:
        radial-gradient(circle at 15% 20%, rgba(0, 245, 255, 0.05), transparent 35%),
        radial-gradient(circle at 85% 80%, rgba(255, 46, 136, 0.05), transparent 40%);
      pointer-events: none;
      z-index: 0;
    }

    main, header { position: relative; z-index: 1; }
    code, .mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }

    /* — Card primitive — */
    .card {
      position: relative;
      background: linear-gradient(180deg, var(--ink-1) 0%, var(--ink-0) 100%);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 1.1rem 1.2rem;
      overflow: hidden;
    }
    .card::before {
      content: ''; position: absolute; inset: 0 0 auto 0; height: 2px;
      background: linear-gradient(90deg, var(--card-accent, transparent), transparent 70%);
      opacity: 0.85;
    }
    .card[data-cat=recovery]  { --card-accent: var(--cyan); }
    .card[data-cat=training]  { --card-accent: var(--lime); }
    .card[data-cat=nutrition] { --card-accent: var(--amber); }
    .card[data-cat=alerts]    { --card-accent: var(--magenta); }
    .card[data-cat=sleep]     { --card-accent: var(--violet); }

    /* — Typography — */
    .stat-num {
      font-family: 'Inter', sans-serif;
      font-weight: 800;
      font-size: 2.6rem;
      line-height: 1;
      letter-spacing: -0.02em;
    }
    .stat-num-sm { font-size: 1.6rem; font-weight: 700; line-height: 1; letter-spacing: -0.01em; }
    .stat-label {
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.14em;
    }
    .display-num {
      font-family: 'Inter', sans-serif;
      font-weight: 900;
      font-size: 5.5rem;
      line-height: 0.9;
      letter-spacing: -0.04em;
    }

    /* — Pills / badges — */
    .pill {
      display: inline-flex; align-items: center; gap: 0.35rem;
      padding: 0.2rem 0.6rem;
      border-radius: 999px;
      font-size: 0.7rem; font-weight: 700;
      letter-spacing: 0.06em; text-transform: uppercase;
      border: 1px solid;
    }
    .pill-cyan    { color: var(--cyan);    border-color: rgba(0,245,255,0.4);    background: rgba(0,245,255,0.08); }
    .pill-lime    { color: var(--lime);    border-color: rgba(173,250,60,0.4);   background: rgba(173,250,60,0.08); }
    .pill-amber   { color: var(--amber);   border-color: rgba(255,179,0,0.4);    background: rgba(255,179,0,0.10); }
    .pill-magenta { color: var(--magenta); border-color: rgba(255,46,136,0.4);   background: rgba(255,46,136,0.08); }
    .pill-muted   { color: var(--muted);   border-color: var(--line);            background: transparent; }

    /* Glassy alert chip backgrounds (used by ALERT_COLORS) */
    .border-magenta { border-color: rgba(255,46,136,0.5); }
    .border-amber   { border-color: rgba(255,179,0,0.5); }
    .border-lime    { border-color: rgba(173,250,60,0.5); }
    .text-magenta-200 { color: #ffb6d1; }
    .text-amber-200   { color: #ffd97a; }
    .text-lime-200    { color: #d3f487; }
    .bg-magenta-glass { background: rgba(255,46,136,0.07); }
    .bg-amber-glass   { background: rgba(255,179,0,0.07); }
    .bg-lime-glass    { background: rgba(173,250,60,0.07); }

    /* — Glow effect — */
    .glow-cyan    { filter: drop-shadow(0 0 6px rgba(0,245,255,0.6)); }
    .glow-lime    { filter: drop-shadow(0 0 6px rgba(173,250,60,0.55)); }
    .glow-amber   { filter: drop-shadow(0 0 6px rgba(255,179,0,0.55)); }
    .glow-magenta { filter: drop-shadow(0 0 6px rgba(255,46,136,0.55)); }

    /* — Sparkline — */
    .sparkline-wrap { position: absolute; left: 0; right: 0; bottom: 0; height: 38px; opacity: 0.7; pointer-events: none; }
    canvas { max-height: 320px; }

    /* — Year heatmap — */
    .heatmap-cell {
      width: 11px; height: 11px;
      border-radius: 2px;
      transition: transform 0.12s ease;
    }
    .heatmap-cell:hover { transform: scale(1.6); outline: 1px solid var(--cyan); }

    /* — Meal timing strip — */
    .timing-strip {
      position: relative;
      height: 56px;
      background: linear-gradient(90deg, var(--ink-2) 0%, var(--ink-0) 100%);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
    }
    .timing-mark {
      position: absolute;
      top: 50%; transform: translate(-50%, -50%);
      width: 14px; height: 14px;
      border-radius: 999px;
      background: radial-gradient(circle, var(--amber) 0%, rgba(255,179,0,0.2) 70%, transparent 100%);
      box-shadow: 0 0 12px rgba(255,179,0,0.7);
    }
    .timing-tick {
      position: absolute; top: 0; bottom: 0; width: 1px;
      background: rgba(255,255,255,0.05);
    }

    /* — Macro ring — */
    .ring-track  { stroke: var(--ink-3); }
    .ring-fill-p { stroke: var(--lime);  filter: drop-shadow(0 0 4px rgba(173,250,60,0.6)); }
    .ring-fill-c { stroke: var(--amber); filter: drop-shadow(0 0 4px rgba(255,179,0,0.6)); }
    .ring-fill-f { stroke: var(--magenta); filter: drop-shadow(0 0 4px rgba(255,46,136,0.55)); }
    .ring-fill-readiness { stroke: var(--cyan); filter: drop-shadow(0 0 8px rgba(0,245,255,0.65)); }

    /* — Top-line title — */
    h1 .accent { color: var(--cyan); text-shadow: 0 0 8px rgba(0,245,255,0.4); }
    h2.section-title {
      font-size: 0.78rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.18em;
      color: var(--muted);
      margin-bottom: 0.5rem;
    }
    h2.section-title::before {
      content: ''; display: inline-block;
      width: 24px; height: 1px;
      background: currentColor; vertical-align: middle;
      margin-right: 0.5rem;
    }

    /* — Info button on each card — */
    .info-btn {
      position: absolute;
      top: 12px; right: 12px;
      width: 22px; height: 22px;
      display: inline-flex; align-items: center; justify-content: center;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      color: var(--muted);
      font-size: 11px; font-weight: 700;
      cursor: pointer;
      transition: all 0.15s ease;
      z-index: 5;
    }
    .info-btn:hover {
      color: var(--cyan);
      border-color: rgba(0,245,255,0.5);
      background: rgba(0,245,255,0.08);
      box-shadow: 0 0 8px rgba(0,245,255,0.4);
    }

    /* — Slide-in info panel — */
    .info-overlay {
      position: fixed; inset: 0;
      background: rgba(2, 4, 10, 0.6);
      backdrop-filter: blur(2px);
      z-index: 50;
      opacity: 0; pointer-events: none;
      transition: opacity 0.2s ease;
    }
    .info-overlay.open { opacity: 1; pointer-events: auto; }

    .info-sheet {
      position: fixed; top: 0; right: 0; bottom: 0;
      width: min(440px, 100vw);
      background: linear-gradient(180deg, var(--ink-1) 0%, var(--ink-0) 100%);
      border-left: 1px solid var(--line);
      box-shadow: -20px 0 50px rgba(0,0,0,0.5);
      z-index: 51;
      transform: translateX(100%);
      transition: transform 0.32s cubic-bezier(.22,.94,.30,1);
      overflow-y: auto;
      padding: 1.6rem 1.5rem 3rem;
    }
    .info-sheet.open { transform: translateX(0); }
    .info-sheet::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--cyan), var(--magenta) 60%, var(--amber));
      filter: drop-shadow(0 0 6px rgba(0,245,255,0.5));
    }
    .info-sheet h3 {
      font-size: 1.4rem; font-weight: 800; line-height: 1.15;
      letter-spacing: -0.01em;
      margin-bottom: 0.25rem;
    }
    .info-sheet .info-section {
      margin-top: 1.4rem;
      padding-top: 1rem;
      border-top: 1px solid var(--line);
    }
    .info-sheet .info-section-label {
      font-size: 0.65rem;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      color: var(--muted);
      font-weight: 700;
      margin-bottom: 0.5rem;
      display: flex; align-items: center; gap: 0.5rem;
    }
    .info-sheet .info-section-label::before {
      content: ''; width: 14px; height: 1px;
      background: currentColor;
    }
    .info-sheet p { margin-top: 0.5rem; line-height: 1.55; color: var(--text); }
    .info-sheet p:first-child { margin-top: 0; }
    .info-sheet ul { margin-top: 0.5rem; }
    .info-sheet li { line-height: 1.55; color: var(--text); }
    .info-sheet code {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85em;
      background: var(--ink-3);
      padding: 1px 5px;
      border-radius: 3px;
      color: var(--cyan);
    }
    .info-sheet a {
      color: var(--cyan);
      text-decoration: underline;
      text-decoration-color: rgba(0,245,255,0.4);
      text-underline-offset: 3px;
    }
    .info-sheet a:hover { text-decoration-color: var(--cyan); }
    .info-close {
      position: absolute; top: 14px; right: 14px;
      width: 32px; height: 32px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font-size: 18px; line-height: 1;
    }
    .info-close:hover { color: var(--magenta); border-color: rgba(255,46,136,0.5); }
    .insight-block {
      background: rgba(0,245,255,0.05);
      border: 1px solid rgba(0,245,255,0.2);
      border-radius: 10px;
      padding: 0.9rem 1rem;
    }
    .insight-block p { color: #d8eef5; }
  </style>
</head>
<body class="min-h-screen">
  <header class="px-6 py-4 border-b border-[color:var(--line)] flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-extrabold tracking-tight">VITALS<span class="accent">.</span></h1>
      <p class="text-xs mono text-[color:var(--muted)] mt-0.5">
        host <span class="text-[color:var(--text)]">__HOSTNAME__</span> ·
        last sync <span id="last-refresh" class="text-[color:var(--text)]">—</span>
      </p>
    </div>
    <button onclick="window.location.reload()"
            class="pill pill-cyan hover:bg-[rgba(0,245,255,0.15)]">
      ↻ Refresh
    </button>
  </header>

  <main class="px-4 md:px-6 py-6 max-w-7xl mx-auto space-y-5"
        hx-trigger="load, every 5m" hx-get="/partials/all" hx-target="#root">
    <div id="root" class="text-[color:var(--muted)] mono text-xs">// loading vitals…</div>
  </main>

  <!-- Slide-in info panel — populated from #metric-info-data on click -->
  <div id="info-overlay" class="info-overlay" data-info-close></div>
  <aside id="info-sheet" class="info-sheet" role="dialog" aria-modal="true" aria-labelledby="info-title">
    <button class="info-close" data-info-close aria-label="Close info panel">×</button>
    <h3 id="info-title">—</h3>
    <p id="info-tagline" class="text-xs text-[color:var(--muted)] mono"></p>

    <section class="info-section">
      <div class="info-section-label">What it is</div>
      <div id="info-what"></div>
    </section>

    <section class="info-section">
      <div class="info-section-label">Insight · your data</div>
      <div id="info-insight" class="insight-block"></div>
    </section>

    <section class="info-section">
      <div class="info-section-label">Sources</div>
      <ul id="info-sources" class="list-disc pl-5 text-sm space-y-1"></ul>
    </section>
  </aside>

  <script>
    document.body.addEventListener('htmx:afterSwap', () => {
      document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
      renderAll();
    });

    // — Info-panel wiring — open on info-button click, close on overlay/Esc/×
    document.body.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-info]');
      if (btn) {
        e.preventDefault();
        openInfo(btn.dataset.info);
        return;
      }
      if (e.target.closest('[data-info-close]')) {
        closeInfo();
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeInfo();
    });

    function readInfoData() {
      const el = document.getElementById('metric-info-data');
      if (!el) return {};
      try { return JSON.parse(el.textContent); } catch { return {}; }
    }

    function openInfo(metricId) {
      const data = readInfoData()[metricId];
      const sheet = document.getElementById('info-sheet');
      const overlay = document.getElementById('info-overlay');
      if (!data) {
        document.getElementById('info-title').textContent = 'Info coming soon';
        document.getElementById('info-tagline').textContent = `metric: ${metricId}`;
        document.getElementById('info-what').innerHTML =
          '<p>This card does not yet have an info entry. Add one to <code>src/metric_info.py</code>.</p>';
        document.getElementById('info-insight').innerHTML = '';
        document.getElementById('info-sources').innerHTML = '';
      } else {
        document.getElementById('info-title').textContent = data.title;
        document.getElementById('info-tagline').textContent = `metric_id: ${metricId}`;
        document.getElementById('info-what').innerHTML = data.what || '';
        document.getElementById('info-insight').innerHTML = data.insight || '';
        document.getElementById('info-sources').innerHTML = (data.sources || [])
          .map(s => `<li><a href="${s.url}" target="_blank" rel="noopener noreferrer">${s.title}</a></li>`)
          .join('');
      }
      sheet.classList.add('open');
      overlay.classList.add('open');
      document.body.style.overflow = 'hidden';
    }

    function closeInfo() {
      document.getElementById('info-sheet').classList.remove('open');
      document.getElementById('info-overlay').classList.remove('open');
      document.body.style.overflow = '';
    }

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

    // — CountUp pass over every [data-countup] element after each swap —
    function animateNumbers() {
      document.querySelectorAll('[data-countup]').forEach(el => {
        const target = parseFloat(el.dataset.countup);
        if (isNaN(target)) return;
        const decimals = parseInt(el.dataset.decimals || '0', 10);
        const cu = new countUp.CountUp(el, target, {
          duration: 1.0, decimalPlaces: decimals, separator: ',', useEasing: true,
        });
        if (!cu.error) cu.start();
      });
    }

    // — Stroke-dasharray progress for SVG rings declared in markup —
    function animateRings() {
      document.querySelectorAll('[data-ring-pct]').forEach(el => {
        const pct = Math.max(0, Math.min(1, parseFloat(el.dataset.ringPct)));
        const r = parseFloat(el.getAttribute('r'));
        const circ = 2 * Math.PI * r;
        // On re-swaps a transition from the previous run would animate the
        // reset itself (visible rewind) — disable it, commit the reset, then
        // re-enable before animating to the target offset.
        el.style.transition = 'none';
        el.style.strokeDasharray = circ;
        el.style.strokeDashoffset = circ;
        // Round linecaps render a floating dot even at zero length — hide.
        el.style.opacity = pct > 0 ? '' : '0';
        el.getBoundingClientRect();
        requestAnimationFrame(() => {
          el.style.transition = 'stroke-dashoffset 1.1s cubic-bezier(.6,.1,.2,1)';
          el.style.strokeDashoffset = circ * (1 - pct);
        });
      });
    }

    function renderAll() {
      animateNumbers();
      animateRings();
      renderTrend('hrvTrendChart', readJSON('hrvTrendData'), 'HRV (ms)', getCSS('--cyan'));
      renderTrend('rhrTrendChart', readJSON('rhrTrendData'), 'Resting HR (bpm)', getCSS('--magenta'));
      renderSleepStages();
      renderSleepDuration();
      renderTrainingLoad();
      renderBodyBatterySpark();
      renderStatSpark('rhrSpark', readJSON('rhrSparkData'), getCSS('--magenta'));
      renderStatSpark('hrvSpark', readJSON('hrvSparkData'), getCSS('--cyan'));
      renderStatSpark('stepsSpark', readJSON('stepsSparkData'), getCSS('--lime'));
      renderStatSpark('sleepSpark', readJSON('sleepSparkData'), getCSS('--violet'));
      renderZ2Chart();
      renderSleepDebtChart();
      renderBalanceChart();
      renderMacrosLegacy();
    }

    function getCSS(varName) {
      return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    }

    function renderStatSpark(canvasId, data, color) {
      destroyChart(canvasId);
      const ctx = $(canvasId);
      if (!ctx || !data) return;
      charts[canvasId] = new Chart(ctx, {
        type: 'line',
        data: {
          labels: data.map(r => r.date),
          datasets: [{
            data: data.map(r => r.value),
            borderColor: color,
            backgroundColor: color + '22',
            borderWidth: 1.4,
            pointRadius: 0,
            tension: 0.4,
            fill: true,
            spanGaps: true,
          }],
        },
        options: {
          animation: false,
          plugins: { legend: { display: false }, tooltip: { enabled: false } },
          scales: { x: { display: false }, y: { display: false } },
          maintainAspectRatio: false,
        },
      });
    }

    function renderMacrosLegacy() { /* macro rings replaced by SVG; renderer kept as no-op for compat */ }

    function renderZ2Chart() {
      const data = readJSON('z2Data');
      destroyChart('z2Chart');
      const ctx = $('z2Chart');
      if (!ctx || !data) return;
      charts.z2Chart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.by_day.map(d => d.date.slice(5)),
          datasets: [{
            label: 'Z2 minutes',
            data: data.by_day.map(d => d.minutes),
            backgroundColor: getCSS('--lime'),
            borderRadius: 3,
          }],
        },
        options: {
          animation: { duration: 600 },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { title: (items) => items[0].label } },
          },
          scales: {
            x: { ticks: { color: 'rgba(232,237,242,0.5)' }, grid: { display: false } },
            y: { ticks: { color: 'rgba(232,237,242,0.5)' }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
          maintainAspectRatio: false,
        },
      });
    }

    function renderSleepDebtChart() {
      const data = readJSON('sleepDebtData');
      destroyChart('sleepDebtChart');
      const ctx = $('sleepDebtChart');
      if (!ctx || !data) return;
      const bars = data.by_day.map(d => d.deficit_h);
      const colors = bars.map(v => v == null ? 'rgba(106,114,130,0.3)' : (v > 0 ? getCSS('--magenta') : getCSS('--lime')));
      charts.sleepDebtChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.by_day.map(d => d.date.slice(5)),
          datasets: [{
            data: bars,
            backgroundColor: colors,
            borderRadius: 3,
          }],
        },
        options: {
          animation: { duration: 600 },
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: 'rgba(232,237,242,0.5)' }, grid: { display: false } },
            y: {
              ticks: { color: 'rgba(232,237,242,0.5)', callback: v => v + 'h' },
              grid: { color: 'rgba(255,255,255,0.04)' },
              suggestedMin: -3, suggestedMax: 3,
            },
          },
          maintainAspectRatio: false,
        },
      });
    }

    function renderBalanceChart() {
      const data = readJSON('balanceData');
      destroyChart('balanceChart');
      const ctx = $('balanceChart');
      if (!ctx || !data) return;
      const bars = data.map(d => d.balance_kcal);
      const colors = bars.map(v => v >= 0 ? getCSS('--amber') : getCSS('--cyan'));
      const avg = rolling(bars, 7);
      charts.balanceChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: data.map(d => d.date.slice(5)),
          datasets: [
            {
              type: 'bar', label: 'Balance',
              data: bars, backgroundColor: colors, borderRadius: 3,
            },
            {
              type: 'line', label: '7-day avg',
              data: avg, borderColor: getCSS('--text'),
              borderDash: [4, 4], borderWidth: 1,
              pointRadius: 0, tension: 0.3, fill: false,
            },
          ],
        },
        options: {
          animation: { duration: 600 },
          plugins: { legend: { labels: { color: 'rgba(232,237,242,0.7)' } } },
          scales: {
            x: { ticks: { color: 'rgba(232,237,242,0.5)' }, grid: { display: false } },
            y: { ticks: { color: 'rgba(232,237,242,0.5)' }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
          maintainAspectRatio: false,
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
            borderWidth: 0,
            spacing: 2,
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
    # Fall back to most recent day with actual data if today is empty
    today = db.daily_summary_for(db_path, _today_iso()) or {}
    if not any(today.get(k) for k in ("resting_hr", "steps", "hrv_overnight")):
        import sqlite3
        con = sqlite3.connect(db_path)
        row = con.execute(
            "SELECT date FROM daily_summary WHERE resting_hr IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row:
            today = db.daily_summary_for(db_path, row[0]) or {}
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


def _info_btn(metric_id: str) -> str:
    """Renders the discrete ⓘ button that opens the side panel for a given metric."""
    return f'<button class="info-btn" data-info="{metric_id}" aria-label="About this metric" title="About">i</button>'


def _spark_card(
    label: str,
    value: str,
    sub: str,
    canvas_id: str,
    spark_data: list[dict],
    category: str = "recovery",
    countup_target: float | None = None,
    decimals: int = 0,
    info_id: str | None = None,
) -> str:
    """Stat card with a 14-day sparkline behind the headline number."""
    spark_id = canvas_id + "Data"
    cu_attr = ""
    inner_value = value
    if countup_target is not None:
        cu_attr = f' data-countup="{countup_target}" data-decimals="{decimals}"'
        inner_value = "0"
    info_html = _info_btn(info_id) if info_id else ""
    return f"""
    <div class="card relative overflow-hidden" data-cat="{category}">
      {info_html}
      <div class="stat-label">{label}</div>
      <div class="stat-num mt-1"><span{cu_attr}>{inner_value}</span></div>
      <div class="text-xs text-[color:var(--muted)] mt-1">{sub}</div>
      <div class="sparkline-wrap">
        <script type="application/json" id="{spark_id}">{json.dumps(spark_data)}</script>
        <canvas id="{canvas_id}"></canvas>
      </div>
    </div>
    """


def _hero_html(readiness: dict, summary: dict) -> str:
    """Big readiness ring + recommendation copy + three sub-stats."""
    score = readiness.get("score")
    band = readiness.get("band") or "unknown"
    components = readiness.get("components") or {}

    if score is None:
        score_display = "—"
        pct = 0.0
        rec_label = "AWAITING DATA"
        rec_sub = "Need ≥1 day of summary to score readiness."
        pill_class = "pill-muted"
    else:
        score_display = str(score)
        pct = max(0.0, min(1.0, score / 100.0))
        rec_label, rec_sub = {
            "high":   ("PEAK · TRAIN HARD",  "Body's primed. Push the intensity today."),
            "medium": ("STEADY · MAINTAIN",  "Solid recovery — train as planned."),
            "low":    ("RED · RECOVER",      "Recovery debt. Prioritize sleep & easy work."),
        }.get(band, ("BUILDING BASELINE", ""))
        pill_class = {"high": "pill-lime", "medium": "pill-cyan", "low": "pill-magenta"}.get(band, "pill-muted")

    def _delta_pill(comp_key: str, label: str) -> str:
        v = components.get(comp_key)
        if v is None:
            return f'<div class="pill pill-muted">{label} —</div>'
        sign = "+" if v >= 0 else ""
        cls = "pill-lime" if v > 0.05 else ("pill-magenta" if v < -0.05 else "pill-cyan")
        return f'<div class="pill {cls}">{label} {sign}{int(round(v * 100))}%</div>'

    return f"""
    <section class="card" data-cat="recovery">
      {_info_btn("readiness")}
      <div class="flex flex-col md:flex-row md:items-center gap-6">
        <!-- Readiness ring -->
        <div class="relative flex items-center justify-center" style="width:240px;height:240px">
          <svg viewBox="0 0 240 240" width="240" height="240" class="-rotate-90 glow-cyan">
            <circle class="ring-track" cx="120" cy="120" r="100" stroke-width="14" fill="none"></circle>
            <circle class="ring-fill-readiness" cx="120" cy="120" r="100" stroke-width="14"
                    fill="none" stroke-linecap="round"
                    data-ring-pct="{pct:.4f}"></circle>
          </svg>
          <div class="absolute inset-0 flex flex-col items-center justify-center">
            <div class="display-num">
              <span data-countup="{score if score is not None else 0}" data-decimals="0">0</span>
            </div>
            <div class="stat-label mt-1">Readiness</div>
          </div>
        </div>

        <!-- Right side -->
        <div class="flex-1 space-y-4">
          <div>
            <div class="flex items-center gap-3">
              <div class="pill {pill_class}">{rec_label}</div>
            </div>
            <p class="text-sm text-[color:var(--muted)] mt-2">{rec_sub}</p>
          </div>
          <div class="flex flex-wrap gap-2">
            {_delta_pill("hrv", "HRV")}
            {_delta_pill("rhr", "RHR")}
            {_delta_pill("sleep", "SLEEP")}
            {_delta_pill("bb", "BODY BATT")}
          </div>
          <div class="text-xs mono text-[color:var(--muted)]">
            for {summary.get('date') or _today_iso()} · weights: hrv 0.5 · rhr 0.2 · sleep 0.2 · bb 0.1
          </div>
        </div>
      </div>
    </section>
    """


def _stats_row_html(db_path: Path, summary: dict, trends: list[dict]) -> str:
    """Six stat cards with 14-day sparklines."""
    last14 = trends[-14:]
    rhr_spark = [{"date": r["date"], "value": r["resting_hr"]} for r in last14]
    hrv_spark = [{"date": r["date"], "value": r["hrv_overnight"]} for r in last14]

    with db.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, steps, sleep_seconds FROM daily_summary "
            "ORDER BY date DESC LIMIT 14"
        ).fetchall()
    rows = [dict(r) for r in reversed(rows)]
    steps_spark = [{"date": r["date"], "value": r.get("steps")} for r in rows]
    sleep_spark = [
        {"date": r["date"], "value": (r.get("sleep_seconds") or 0) / 3600.0 if r.get("sleep_seconds") else None}
        for r in rows
    ]
    bb_spark = [{"date": r["date"], "value": r["body_battery"]} for r in last14]

    rhr = summary.get("resting_hr")
    hrv = summary.get("hrv")
    steps = summary.get("steps") or 0
    bb = summary.get("body_battery")
    stress = summary.get("stress_avg")
    sleep_h = (summary.get("sleep_seconds") or 0) / 3600.0 if summary.get("sleep_seconds") else None

    return f"""
    <section class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {_spark_card("Resting HR",
                   value="—" if rhr is None else str(rhr),
                   sub="bpm",
                   canvas_id="rhrSpark", spark_data=rhr_spark, category="recovery",
                   countup_target=float(rhr) if rhr is not None else None,
                   info_id="rhr")}
      {_spark_card("HRV",
                   value="—" if hrv is None else str(hrv),
                   sub="ms · overnight",
                   canvas_id="hrvSpark", spark_data=hrv_spark, category="recovery",
                   countup_target=float(hrv) if hrv is not None else None,
                   info_id="hrv")}
      {_spark_card("Steps",
                   value=f"{steps:,}",
                   sub="today",
                   canvas_id="stepsSpark", spark_data=steps_spark, category="training",
                   countup_target=float(steps),
                   info_id="steps")}
      {_spark_card("Sleep",
                   value="—" if sleep_h is None else f"{sleep_h:.1f}h",
                   sub="last night",
                   canvas_id="sleepSpark", spark_data=sleep_spark, category="sleep",
                   countup_target=sleep_h, decimals=1,
                   info_id="sleep")}
      {_spark_card("Body Batt",
                   value="—" if bb is None else str(bb),
                   sub="current",
                   canvas_id="bbSparkChart", spark_data=bb_spark, category="recovery",
                   countup_target=float(bb) if bb is not None else None,
                   info_id="body_battery")}
      {_spark_card("Stress",
                   value="—" if stress is None else str(stress),
                   sub="avg today",
                   canvas_id="stressSpark", spark_data=[],
                   category="alerts",
                   countup_target=float(stress) if stress is not None else None,
                   info_id="stress")}
    </section>
    """




def _row3_html(trends: list[dict]) -> str:
    hrv_data = [{"date": r["date"], "value": r["hrv_overnight"]} for r in trends]
    rhr_data = [{"date": r["date"], "value": r["resting_hr"]} for r in trends]
    return f"""
    <section class="grid md:grid-cols-2 gap-3">
      <div class="card" data-cat="recovery">
        {_info_btn("hrv_trend")}
        <h2 class="font-semibold mb-2">HRV — last 30d</h2>
        <script type="application/json" id="hrvTrendData">{json.dumps(hrv_data)}</script>
        <canvas id="hrvTrendChart"></canvas>
      </div>
      <div class="card" data-cat="recovery">
        {_info_btn("rhr_trend")}
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
      <div class="card" data-cat="sleep">
        {_info_btn("sleep_stages")}
        <h2 class="font-semibold mb-2">Sleep stages — last night</h2>
        {stages_block}
      </div>
      <div class="card" data-cat="sleep">
        {_info_btn("sleep_duration")}
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
      <div class="card overflow-x-auto" data-cat="training">
        {_info_btn("activities")}
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
      <div class="card" data-cat="training">
        {_info_btn("training_load")}
        <h2 class="font-semibold mb-2">Training load — last 30d (kcal)</h2>
        <script type="application/json" id="trainingLoadData">{json.dumps(cal_load)}</script>
        <canvas id="trainingLoadChart"></canvas>
      </div>
    </section>
    """


def _acwr_gauge_svg(ratio: float | None, band: str | None) -> str:
    """Hand-rolled semicircular ACWR gauge.

    Band arcs are generated from db.ACWR_UNDERTRAINED_MAX / ACWR_OPTIMAL_MAX /
    ACWR_CAUTION_MAX so the drawing always matches the band classification.
    """
    if ratio is None:
        needle_pct = 0.5
        ratio_text = "—"
        band_label = "AWAITING DATA"
        pill_cls = "pill-muted"
    else:
        # Map ratio [0.0, 2.0] → [0, 1] for needle position; clamp.
        needle_pct = max(0.0, min(1.0, ratio / 2.0))
        ratio_text = f"{ratio:.2f}"
        band_label = {
            "undertrained": "UNDERTRAINED",
            "optimal":      "OPTIMAL",
            "caution":      "CAUTION",
            "high_risk":    "HIGH RISK",
        }.get(band or "", band or "—")
        pill_cls = {
            "undertrained": "pill-cyan",
            "optimal":      "pill-lime",
            "caution":      "pill-amber",
            "high_risk":    "pill-magenta",
        }.get(band or "", "pill-muted")

    needle_x, needle_y = _gauge_point(needle_pct)
    band_arcs = "\n      ".join(
        f'<path d="{_gauge_arc_path(lo / _ACWR_GAUGE_MAX, hi / _ACWR_GAUGE_MAX)}" '
        f'stroke="{color}" stroke-width="9" fill="none" opacity="{opacity}"/>'
        for lo, hi, color, opacity in _ACWR_BANDS
    )
    return f"""
    <svg viewBox="0 0 200 110" class="w-full max-w-[280px] mx-auto">
      <!-- band arcs (under-, optimal, caution, danger) -->
      {band_arcs}
      <!-- needle -->
      <line x1="100" y1="100" x2="{needle_x:.1f}" y2="{needle_y:.1f}"
            stroke="#e8edf2" stroke-width="2.5" stroke-linecap="round" filter="drop-shadow(0 0 4px rgba(255,255,255,0.6))"/>
      <circle cx="100" cy="100" r="4" fill="#e8edf2"/>
      <!-- numeric labels -->
      <text x="20" y="108" fill="#6a7282" font-size="8" font-family="JetBrains Mono">0.0</text>
      <text x="100" y="108" fill="#6a7282" font-size="8" font-family="JetBrains Mono" text-anchor="middle">1.0</text>
      <text x="180" y="108" fill="#6a7282" font-size="8" font-family="JetBrains Mono" text-anchor="end">2.0+</text>
    </svg>
    <div class="text-center mt-2">
      <div class="display-num" style="font-size:2.8rem">{ratio_text}</div>
      <div class="pill {pill_cls} mt-2">{band_label}</div>
    </div>
    """


def _training_intel_html(intel: dict) -> str:
    acwr_data = intel.get("acwr") or {}
    monotony_data = intel.get("monotony") or {}
    z2_data = intel.get("z2") or {}
    sleep_debt_data = intel.get("sleep_debt") or {}

    # ACWR
    acwr_ratio = acwr_data.get("ratio")
    acwr_band = acwr_data.get("band")
    acute = acwr_data.get("acute_avg")
    chronic = acwr_data.get("chronic_avg")
    acwr_sub = (
        f"7d avg <span class='mono text-[color:var(--text)]'>{acute:.0f}</span> kcal · "
        f"28d avg <span class='mono text-[color:var(--text)]'>{chronic:.0f}</span> kcal"
        if acwr_ratio is not None and acute is not None and chronic is not None
        else "Need 28 days of activity history."
    )

    # Monotony
    monotony = monotony_data.get("monotony")
    monotony_band = monotony_data.get("band")
    monotony_pill = {
        "varied":      "pill-lime",
        "elevated":    "pill-amber",
        "monotonous":  "pill-magenta",
        "rest":        "pill-muted",
    }.get(monotony_band or "", "pill-muted")
    monotony_value = f"{monotony:.2f}" if monotony is not None else "—"
    monotony_label = {
        "varied":     "VARIED",
        "elevated":   "ELEVATED",
        "monotonous": "MONOTONOUS",
        "rest":       "REST WEEK",
    }.get(monotony_band or "", "AWAITING DATA")
    monotony_caption = (
        "Foster index = mean ÷ std of 7-day load. > 2.0 flags overuse risk."
    )

    # Z2
    z2_min = z2_data.get("minutes", 0)
    z2_goal = z2_data.get("goal_minutes", 150)
    z2_lower = z2_data.get("lower_bpm", 0)
    z2_upper = z2_data.get("upper_bpm", 0)
    z2_pct = min(1.0, z2_min / z2_goal) if z2_goal else 0.0
    z2_pct_pretty = f"{int(round(z2_pct * 100))}%"

    # Sleep debt
    sd_total = sleep_debt_data.get("total_h", 0.0)
    sd_target = sleep_debt_data.get("target_h", 8)
    sd_status_label = "DEBT" if sd_total > 0 else ("SURPLUS" if sd_total < 0 else "BALANCED")
    sd_pill = "pill-magenta" if sd_total > 0 else ("pill-lime" if sd_total < 0 else "pill-cyan")

    return f"""
    <section>
      <h2 class="section-title">Training Intelligence</h2>
      <div class="grid md:grid-cols-2 lg:grid-cols-4 gap-3">
        <!-- ACWR gauge -->
        <div class="card" data-cat="training">
          {_info_btn("acwr")}
          <div class="stat-label">ACWR · Injury Risk</div>
          {_acwr_gauge_svg(acwr_ratio, acwr_band)}
          <div class="text-xs text-[color:var(--muted)] mt-3">{acwr_sub}</div>
        </div>

        <!-- Monotony -->
        <div class="card" data-cat="training">
          {_info_btn("monotony")}
          <div class="stat-label">Training Monotony</div>
          <div class="display-num mt-3" style="font-size:4rem">{monotony_value}</div>
          <div class="pill {monotony_pill} mt-3">{monotony_label}</div>
          <p class="text-xs text-[color:var(--muted)] mt-3">{monotony_caption}</p>
        </div>

        <!-- Z2 minutes -->
        <div class="card" data-cat="training">
          {_info_btn("z2")}
          <div class="stat-label">Z2 Aerobic · This Week</div>
          <div class="flex items-baseline gap-2 mt-3">
            <span class="display-num" style="font-size:3rem">
              <span data-countup="{z2_min}">0</span>
            </span>
            <span class="text-sm text-[color:var(--muted)]">/ {z2_goal} min · {z2_pct_pretty}</span>
          </div>
          <div class="mt-3 h-1.5 bg-[color:var(--ink-3)] rounded">
            <div class="h-1.5 rounded glow-lime"
                 style="background:var(--lime); width:{z2_pct * 100:.1f}%"></div>
          </div>
          <div style="height:90px" class="mt-3">
            <script type="application/json" id="z2Data">{json.dumps(z2_data)}</script>
            <canvas id="z2Chart"></canvas>
          </div>
          <div class="text-[0.65rem] mono text-[color:var(--muted)] mt-1">
            zone {z2_lower}–{z2_upper} bpm · 60–70% HRmax
          </div>
        </div>

        <!-- Sleep debt -->
        <div class="card" data-cat="sleep">
          {_info_btn("sleep_debt")}
          <div class="stat-label">Sleep Debt · 7d</div>
          <div class="flex items-baseline gap-2 mt-3">
            <span class="display-num" style="font-size:3rem">
              <span data-countup="{sd_total}" data-decimals="1">0</span>
            </span>
            <span class="text-sm text-[color:var(--muted)]">h vs {sd_target}h target</span>
          </div>
          <div class="pill {sd_pill} mt-2">{sd_status_label}</div>
          <div style="height:90px" class="mt-3">
            <script type="application/json" id="sleepDebtData">{json.dumps(sleep_debt_data)}</script>
            <canvas id="sleepDebtChart"></canvas>
          </div>
        </div>
      </div>
    </section>
    """


def _year_heatmap_html(history: list[dict]) -> str:
    """52-week × 7-day grid, GitHub-contribution-graph style."""
    # Group by ISO week from latest date back 365 days.
    # Layout: 7 rows (weekday), 53 cols.
    if not history:
        return ""
    cells = []
    # Build a per-date map for quick lookup
    score_by_date = {h["date"]: h.get("score") for h in history}

    end = datetime.fromisoformat(history[-1]["date"]).date()
    # Walk back to a Sunday so columns are aligned weeks
    days_back = len(history)
    start = end - timedelta(days=days_back - 1)
    # Pad to start on Sunday for column alignment
    pre_pad = (start.weekday() + 1) % 7  # Sunday=0
    start = start - timedelta(days=pre_pad)
    cur = start
    cols = []
    while cur <= end:
        col = []
        for _ in range(7):
            score = score_by_date.get(cur.isoformat())
            if score is None:
                color = _HEATMAP_PALETTE[0]
            else:
                # 7 buckets above the empty one
                bucket = min(7, max(1, round(score / 100 * 7)))
                color = _HEATMAP_PALETTE[bucket]
            col.append(
                f'<div class="heatmap-cell" style="background:{color}" '
                f'title="{cur.isoformat()} · score {score if score is not None else "—"}"></div>'
            )
            cur += timedelta(days=1)
        cols.append("<div class='flex flex-col gap-[2px]'>" + "".join(col) + "</div>")
        if cur > end:
            break
    legend_swatches = "".join(
        f'<div class="heatmap-cell" style="background:{c}"></div>'
        for c in _HEATMAP_PALETTE
    )
    return f"""
    <section>
      <h2 class="section-title">Annual Readiness · {len(history)}d</h2>
      <div class="card" data-cat="recovery">
        {_info_btn("heatmap")}
        <div class="overflow-x-auto">
          <div class="flex gap-[2px] min-w-fit">
            {''.join(cols)}
          </div>
        </div>
        <div class="flex items-center gap-2 mt-3 text-xs text-[color:var(--muted)]">
          <span>low</span>
          <div class="flex gap-[2px]">{legend_swatches}</div>
          <span>high</span>
        </div>
      </div>
    </section>
    """


def _meal_time_local(meal_time: str | None) -> str:
    if not meal_time:
        return "—"
    try:
        return datetime.fromisoformat(meal_time).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return meal_time[11:16] if len(meal_time) >= 16 else meal_time


def _macro_ring_svg(label: str, current_g: float, target_g: float, ring_class: str) -> str:
    pct = max(0.0, min(1.0, current_g / target_g)) if target_g else 0.0
    return f"""
    <div class="flex flex-col items-center">
      <div class="relative" style="width:120px;height:120px">
        <svg viewBox="0 0 120 120" width="120" height="120" class="-rotate-90">
          <circle class="ring-track" cx="60" cy="60" r="50" stroke-width="9" fill="none"></circle>
          <circle class="{ring_class}" cx="60" cy="60" r="50" stroke-width="9" fill="none"
                  stroke-linecap="round" data-ring-pct="{pct:.4f}"></circle>
        </svg>
        <div class="absolute inset-0 flex flex-col items-center justify-center">
          <div class="stat-num-sm"><span data-countup="{current_g:.0f}">0</span></div>
          <div class="text-[0.6rem] mono text-[color:var(--muted)]">/ {target_g:.0f}g</div>
        </div>
      </div>
      <div class="stat-label mt-2">{label}</div>
    </div>
    """


def _gauge_bar(label: str, current: float, target: float, danger_above: bool = False) -> str:
    pct_raw = current / target if target else 0
    pct = max(0.0, min(1.0, pct_raw))
    over = pct_raw > 1.0 and danger_above
    bar_color = "var(--magenta)" if over else "var(--cyan)"
    bar_glow = "glow-magenta" if over else "glow-cyan"
    return f"""
    <div>
      <div class="flex items-baseline justify-between">
        <span class="stat-label">{label}</span>
        <span class="text-xs mono">
          <span class="text-[color:var(--text)]">{current:.0f}</span>
          <span class="text-[color:var(--muted)]">/ {target:.0f}</span>
        </span>
      </div>
      <div class="h-2 mt-1 bg-[color:var(--ink-3)] rounded overflow-hidden">
        <div class="h-2 rounded {bar_glow}" style="background:{bar_color};width:{pct*100:.1f}%"></div>
      </div>
    </div>
    """


def _meal_timing_strip(meals: list[dict], timing: dict | None) -> str:
    """Horizontal 24h timeline with meal dots and the active fasting window highlighted."""
    if not meals:
        return (
            '<div class="timing-strip flex items-center justify-center text-xs '
            'text-[color:var(--muted)]">No meals logged · 24h timeline</div>'
        )
    # Convert each meal_time → fraction of day (local).
    marks_html = []
    for m in meals:
        try:
            t = datetime.fromisoformat(m["meal_time"]).astimezone()
            frac = (t.hour * 3600 + t.minute * 60 + t.second) / 86400.0
        except (TypeError, ValueError, KeyError):
            continue
        title = f'{t.strftime("%H:%M")} · {m.get("description") or ""}'
        marks_html.append(
            f'<div class="timing-mark" style="left:{frac*100:.2f}%" title="{title}"></div>'
        )
    ticks = "".join(
        f'<div class="timing-tick" style="left:{i/24*100:.2f}%"></div>'
        for i in range(1, 24)
    )
    fasting_html = ""
    if timing:
        fasting_html = (
            f'<div class="text-xs mono text-[color:var(--muted)] mt-1">'
            f'first {timing["first_meal_local"]} · last {timing["last_meal_local"]} · '
            f'eating window {timing["eating_window_h"]:.1f}h · '
            f'<span class="text-[color:var(--amber)]">{timing["hours_since_last_meal"]:.1f}h since last</span>'
            f'</div>'
        )
    return f"""
    <div>
      <div class="timing-strip">
        {ticks}
        {''.join(marks_html)}
      </div>
      <div class="flex justify-between text-[0.6rem] mono text-[color:var(--muted)] mt-1">
        <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span>
      </div>
      {fasting_html}
    </div>
    """


def _energy_availability_card(ea: dict) -> str:
    status = ea.get("status")
    ea_value = ea.get("ea_kcal_per_kg")
    if status == "unknown" or ea_value is None:
        return f"""
        <div class="card" data-cat="nutrition">
          {_info_btn("energy_availability")}
          <div class="stat-label">Energy Availability</div>
          <div class="text-sm text-[color:var(--muted)] mt-3">
            Log meals to compute EA (kcal/kg LBM) and the RED-S threshold.
          </div>
        </div>
        """
    label = {"optimal": "OPTIMAL", "low": "LOW", "red_s": "RED-S WARNING"}.get(status, status.upper())
    pill = {"optimal": "pill-lime", "low": "pill-amber", "red_s": "pill-magenta"}.get(status, "pill-muted")
    glow = {"optimal": "glow-lime", "low": "glow-amber", "red_s": "glow-magenta"}.get(status, "")
    note = {
        "optimal": "≥ 45 kcal/kg LBM — physiologically supportive.",
        "low": "30–45 kcal/kg LBM — adaptation may suffer if sustained.",
        "red_s": "< 30 kcal/kg LBM — IOC RED-S threshold. Eat more.",
    }.get(status, "")
    return f"""
    <div class="card" data-cat="nutrition">
      {_info_btn("energy_availability")}
      <div class="stat-label">Energy Availability</div>
      <div class="display-num mt-3 {glow}" style="font-size:3.4rem">
        <span data-countup="{ea_value}" data-decimals="1">0</span>
        <span class="text-base text-[color:var(--muted)] ml-1">kcal/kg</span>
      </div>
      <div class="pill {pill} mt-2">{label}</div>
      <p class="text-xs text-[color:var(--muted)] mt-3">{note}</p>
      <p class="text-[0.65rem] mono text-[color:var(--muted)] mt-1">
        LBM est. {ea.get('lbm_kg', 0):.1f}kg · activity {ea.get('activity_kcal', 0):.0f}kcal
      </p>
    </div>
    """


def _nutrition_v2_html(
    balance: dict,
    meals: list[dict],
    nutrition_extras: dict,
    history_7d: list[dict],
) -> str:
    eaten = balance.get("eaten_kcal") or 0
    bmr = balance.get("bmr_kcal") or 0
    steps_burned = balance.get("steps_burned_kcal") or 0
    activity_burned = balance.get("burned_kcal") or 0
    total_burned = balance.get("total_burned_kcal") or 0
    bal = balance.get("balance_kcal") or 0
    bal_pill = "pill-amber" if bal >= 0 else "pill-cyan"
    bal_label = f"{bal:+.0f} kcal · " + ("SURPLUS" if bal >= 0 else "DEFICIT")

    protein_g = balance.get("protein_g") or 0
    carbs_g = balance.get("carbs_g") or 0
    fat_g = balance.get("fat_g") or 0

    p_target = nutrition_extras.get("protein_target_g") or 1
    f_target = nutrition_extras.get("fiber_target_g") or 30
    sodium_target = nutrition_extras.get("sodium_target_mg") or 2300

    # Aggregate fiber + sodium from meals (column may be NULL).
    fiber_total = sum((m.get("fiber_g") or 0) for m in meals)
    sodium_total = sum((m.get("sodium_mg") or 0) for m in meals)

    # Carbs target ≈ 50% of kcal target / 4 ; fat target ≈ 25% / 9. Rough but useful.
    kcal_target = nutrition_extras.get("kcal_target") or 2200
    c_target = max(1, int(kcal_target * 0.50 / 4))
    f_g_target = max(1, int(kcal_target * 0.25 / 9))

    whole_pct = nutrition_extras.get("whole_food_pct")
    whole_pill_html = (
        f'<span class="pill pill-lime">{int(round(whole_pct * 100))}% WHOLE FOOD</span>'
        if whole_pct is not None and whole_pct >= 0.7
        else f'<span class="pill pill-amber">{int(round(whole_pct * 100))}% WHOLE FOOD</span>'
        if whole_pct is not None
        else ""
    )

    timing = nutrition_extras.get("meal_timing")
    timing_strip = _meal_timing_strip(meals, timing)

    # Meals table
    if not meals:
        meals_block = '<p class="text-[color:var(--muted)] text-sm">No meals logged today.</p>'
    else:
        items = []
        for m in meals:
            t = _meal_time_local(m.get("meal_time"))
            kcal = m.get("kcal")
            kcal_str = f"{kcal:.0f}" if kcal is not None else "—"
            cat = m.get("food_category") or ""
            items.append(
                f"<tr class='border-t border-[color:var(--line)]'>"
                f"<td class='py-1.5 pr-3 mono text-[color:var(--muted)] w-16'>{t}</td>"
                f"<td class='py-1.5 pr-3'>{m.get('description') or '—'}"
                f"<div class='text-[0.65rem] text-[color:var(--muted)]'>{cat}</div></td>"
                f"<td class='py-1.5 pr-3 mono text-right text-[color:var(--text)]'>{kcal_str}</td>"
                f"</tr>"
            )
        meals_block = (
            "<table class='w-full text-sm'>"
            "<thead><tr class='stat-label text-left'>"
            "<th class='pb-2 pr-3'>Time</th><th class='pb-2 pr-3'>Meal</th>"
            "<th class='pb-2 pr-3 text-right'>kcal</th></tr></thead>"
            f"<tbody>{''.join(items)}</tbody></table>"
        )

    return f"""
    <section>
      <h2 class="section-title">Nutrition · Today</h2>

      <!-- Top row: balance summary + macros -->
      <div class="grid md:grid-cols-3 gap-3">
        <div class="card" data-cat="nutrition">
          {_info_btn("calorie_balance")}
          <div class="stat-label">Calorie Balance</div>
          <div class="display-num mt-3" style="font-size:3.6rem">
            <span data-countup="{eaten:.0f}">0</span>
            <span class="text-base text-[color:var(--muted)] ml-1">kcal in</span>
          </div>
          <div class="pill {bal_pill} mt-2">{bal_label}</div>
          <div class="mt-4 text-xs text-[color:var(--text)] space-y-1">
            <div class="stat-label" style="letter-spacing:0.1em">Burned</div>
            <div class="flex justify-between mono"><span class="text-[color:var(--muted)]">BMR</span><span>{bmr} kcal</span></div>
            <div class="flex justify-between mono"><span class="text-[color:var(--muted)]">Steps</span><span>{steps_burned} kcal</span></div>
            {f"<div class='flex justify-between mono'><span class='text-[color:var(--muted)]'>Activities</span><span>{activity_burned} kcal</span></div>" if activity_burned > 0 else ""}
            <div class="flex justify-between font-semibold mt-1 pt-1 border-t border-[color:var(--line)] mono">
              <span>Total</span><span>{total_burned} kcal</span>
            </div>
          </div>
          {('<div class="mt-3">' + whole_pill_html + '</div>') if whole_pill_html else ""}
        </div>

        <div class="card md:col-span-2" data-cat="nutrition">
          {_info_btn("macros")}
          <div class="stat-label">Macros · Today</div>
          <div class="grid grid-cols-3 gap-2 mt-4">
            {_macro_ring_svg("Protein", protein_g, p_target, "ring-fill-p")}
            {_macro_ring_svg("Carbs",   carbs_g,   c_target, "ring-fill-c")}
            {_macro_ring_svg("Fat",     fat_g,     f_g_target, "ring-fill-f")}
          </div>
          <div class="mt-5 space-y-3">
            {_gauge_bar("Fiber", fiber_total, f_target)}
            {_gauge_bar("Sodium · mg", sodium_total, sodium_target, danger_above=True)}
          </div>
        </div>
      </div>

      <!-- Middle row: EA + meal timing strip -->
      <div class="grid md:grid-cols-2 gap-3 mt-3">
        {_energy_availability_card(nutrition_extras.get("energy_availability") or {})}
        <div class="card" data-cat="nutrition">
          {_info_btn("meal_timing")}
          <div class="stat-label">Meal Timing · 24h</div>
          <div class="mt-3">
            {timing_strip}
          </div>
        </div>
      </div>

      <!-- Bottom row: 7-day balance chart + meals table -->
      <div class="grid md:grid-cols-2 gap-3 mt-3">
        <div class="card" data-cat="nutrition">
          {_info_btn("balance_history")}
          <div class="stat-label">Calorie Balance · 7 Days</div>
          <div style="height:180px" class="mt-3">
            <script type="application/json" id="balanceData">{json.dumps(history_7d)}</script>
            <canvas id="balanceChart"></canvas>
          </div>
        </div>
        <div class="card overflow-x-auto" data-cat="nutrition">
          <div class="stat-label">Meals</div>
          <div class="mt-3">{meals_block}</div>
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
    <section class="card" data-cat="alerts">
      {_info_btn("alerts")}
      <h2 class="font-semibold mb-2">Recent alerts</h2>
      {body}
    </section>
    """


def render_full(cfg: Config) -> str:
    today_iso = _today_iso()
    summary = get_today_summary(cfg.db_path)
    trends = get_trends(cfg.db_path, 30)
    sleep_dur = [
        {"date": r["date"], "hours": round((r["sleep_seconds"] or 0) / 3600, 2)}
        for r in trends[-14:]
    ]
    today_row = db.daily_summary_for(cfg.db_path, today_iso) or {}
    yest_row = db.daily_summary_for(cfg.db_path, _yesterday_iso()) or {}
    stages = _sleep_stages_from_raw(today_row.get("raw_json")) or _sleep_stages_from_raw(
        yest_row.get("raw_json")
    )

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

    readiness_v2 = db.composite_readiness(cfg.db_path, today_iso, cfg)
    intel = {
        "acwr": db.acwr(cfg.db_path, today_iso),
        "monotony": db.training_monotony(cfg.db_path, today_iso),
        "z2": db.z2_minutes_for_week(cfg.db_path, today_iso, cfg),
        "sleep_debt": db.sleep_debt(cfg.db_path, today_iso, cfg),
    }
    heatmap = db.daily_readiness_history(cfg.db_path, days=180, cfg=cfg)

    today_meals = db.meals_for_date(cfg.db_path, today_iso)
    today_balance = db.calorie_balance_for_date(cfg.db_path, today_iso, cfg=cfg)
    nutrition_extras = {
        "protein_target_g": db.protein_target_g(cfg),
        "fiber_target_g": db.fiber_target_g(cfg),
        "sodium_target_mg": db.sodium_target_mg(),
        "kcal_target": cfg.kcal_target,
        "energy_availability": db.energy_availability(cfg.db_path, today_iso, cfg),
        "whole_food_pct": db.whole_food_pct(cfg.db_path, today_iso),
        "meal_timing": db.meal_timing_summary(cfg.db_path, today_iso),
    }
    history_7d = db.recent_calorie_balance(cfg.db_path, days=7, cfg=cfg)

    alerts_list = get_alerts(cfg.db_path, 10)

    info_ctx = {
        "cfg": cfg,
        "summary": summary,
        "readiness": readiness_v2,
        "acwr": intel["acwr"],
        "monotony": intel["monotony"],
        "z2": intel["z2"],
        "sleep_debt": intel["sleep_debt"],
        "balance": today_balance,
        "meals": today_meals,
        "protein_target_g": nutrition_extras["protein_target_g"],
        "fiber_target_g": nutrition_extras["fiber_target_g"],
        "energy_availability": nutrition_extras["energy_availability"],
        "meal_timing": nutrition_extras["meal_timing"],
        "history_7d": history_7d,
        "heatmap": heatmap,
        "activities": activities,
    }
    info_payload = metric_info.build_payload(info_ctx)
    info_block = (
        f'<script type="application/json" id="metric-info-data">'
        f'{json.dumps(info_payload)}</script>'
    )

    return "\n".join(
        [
            info_block,
            _hero_html(readiness_v2, summary),
            _stats_row_html(cfg.db_path, summary, trends),
            _training_intel_html(intel),
            _row3_html(trends),
            _row4_html(stages, sleep_dur),
            _row5_html(activities, cal_load),
            _nutrition_v2_html(today_balance, today_meals, nutrition_extras, history_7d),
            _year_heatmap_html(heatmap),
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

    @app.get("/api/meals")
    def api_meals(date: str | None = None) -> JSONResponse:
        target = date or _today_iso()
        return JSONResponse(db.meals_for_date(cfg.db_path, target))

    @app.get("/api/calorie-balance")
    def api_calorie_balance(date: str | None = None) -> JSONResponse:
        target = date or _today_iso()
        return JSONResponse(db.calorie_balance_for_date(cfg.db_path, target, cfg=cfg))

    # ── Phase 4a: novel-metrics endpoints (consumed by the redesigned UI) ──

    @app.get("/api/readiness-v2")
    def api_readiness_v2(date: str | None = None) -> JSONResponse:
        target = date or _today_iso()
        return JSONResponse(db.composite_readiness(cfg.db_path, target, cfg))

    @app.get("/api/training-intel")
    def api_training_intel(date: str | None = None) -> JSONResponse:
        target = date or _today_iso()
        return JSONResponse({
            "acwr": db.acwr(cfg.db_path, target),
            "monotony": db.training_monotony(cfg.db_path, target),
            "z2": db.z2_minutes_for_week(cfg.db_path, target, cfg),
            "sleep_debt": db.sleep_debt(cfg.db_path, target, cfg),
        })

    @app.get("/api/heatmap")
    def api_heatmap(days: int = 365) -> JSONResponse:
        return JSONResponse(db.daily_readiness_history(cfg.db_path, days, cfg))

    @app.get("/api/nutrition-v2")
    def api_nutrition_v2(date: str | None = None) -> JSONResponse:
        target = date or _today_iso()
        balance = db.calorie_balance_for_date(cfg.db_path, target, cfg=cfg)
        meals = db.meals_for_date(cfg.db_path, target)
        return JSONResponse({
            "balance": balance,
            "meals": meals,
            "protein_target_g": db.protein_target_g(cfg),
            "fiber_target_g": db.fiber_target_g(cfg),
            "sodium_target_mg": db.sodium_target_mg(),
            "energy_availability": db.energy_availability(cfg.db_path, target, cfg),
            "whole_food_pct": db.whole_food_pct(cfg.db_path, target),
            "meal_timing": db.meal_timing_summary(cfg.db_path, target),
            "history_7d": db.recent_calorie_balance(cfg.db_path, 7, cfg),
        })

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the dashboard with uvicorn (blocking)."""
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level=cfg.log_level.lower())
