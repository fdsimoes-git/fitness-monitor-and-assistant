# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Personal Garmin health-data platform on a Raspberry Pi 5. The Garmin Connect poller writes a daily summary, activities, and smart-alert events into one SQLite DB. Nutrition is logged via barcode (Open Food Facts) or manual CLI entries. A FastAPI dashboard renders everything plus a few metrics Garmin doesn't expose raw (composite readiness, ACWR, Z2 minutes, training monotony, sleep debt). Telegram is the only outbound channel.

## Common commands

```bash
# First-time setup
bash scripts/setup.sh
.venv/bin/python -m src.cli init-db

# Garmin auth — run once interactively to cache tokens (handles MFA)
.venv/bin/python auth_setup.py

# Subcommands
.venv/bin/python -m src.cli poll                                   # one Garmin poll + smart alerts
.venv/bin/python -m src.cli digest [--date YYYY-MM-DD]             # daily Telegram summary
.venv/bin/python -m src.cli dashboard --host 0.0.0.0 --port 8000   # needs requirements-web.txt
.venv/bin/python -m src.cli prune --days 90
.venv/bin/python -m src.cli activities --days 14
.venv/bin/python -m src.cli log-meal --desc 'oats' --kcal 350
.venv/bin/python -m src.cli log-barcode 5449000000996 --grams 330
.venv/bin/python -m src.cli meals [--date YYYY-MM-DD]
.venv/bin/python -m src.cli calorie-balance [--date YYYY-MM-DD]
.venv/bin/python -m src.cli test-alert

# Tests (pytest)
.venv/bin/pytest
.venv/bin/pytest tests/test_smart_alerts.py
.venv/bin/pytest tests/test_smart_alerts.py::test_trend_alert_triggers_on_5_day_climb -v
```

The `dashboard` command needs the optional FastAPI/uvicorn deps:
`.venv/bin/pip install -r requirements-web.txt`.

## Architecture (the parts that span files)

### One producer, one consumer pattern

`src/garmin_poller.py` is the only writer of upstream data; `src/db.py` is the only persistence layer; `src/alerts.py` is the only path to Telegram. The DB is the integration surface for everything else (digest, dashboard, smart alerts).

### Smart alerts run **inside** the poller, not separately

`garmin_poller.run_once()` calls `smart_alerts.run_smart_alerts(cfg)` after upserting the daily summary. There is no separate smart-alert service or timer. To change cadence, change the poller timer.

### Cooldowns are persisted, per-alert-kind

`alerts.maybe_alert(kind=…)` reads `last_alert_ts(kind)` from the `alerts` table and suppresses if elapsed < cooldown. Cooldowns survive restarts. Default is `ALERT_COOLDOWN_SECONDS` from env, but daily-readiness checks (`illness_risk`, `training_ready`, `recovery_day`) override it to ~22h via the `cooldown_seconds=` kwarg so they fire at most once per day.

### Smart-alert thresholds are evidence-based — don't tweak casually

Constants at the top of `src/smart_alerts.py` cite the source papers:
- `ILLNESS_RHR_RISE_FRACTION` / `ILLNESS_HRV_DROP_FRACTION` — Buchheit 2014, Plews 2013
- `TRAINING_HRV_FRACTION` — Kiviniemi 2007
- `TREND_DAYS = 5` — strictly-increasing RHR streak
- `HRV_DROP_FRACTION = 0.20` — 20% below 7-day baseline

Each check has a minimum-samples floor (`*_MIN_BASELINE_SAMPLES`) so a fresh DB doesn't generate false positives.

### Garmin poller fetches four endpoints, individually wrapped

`fetch_daily()` calls `get_stats`, `get_sleep_data`, `get_stress_data`, `get_hrv_data` — each in its own try/except so one failure (HRV often missing on older accounts) doesn't abort the poll. There's a fallback in HRV normalization for accounts that surface `hrvLastNightAvg` on the stats payload directly.

`get_activities(0, 10)` is called once per poll and rows are deduped via `upsert_activity` keyed on `activity_id`.

### Schema lives in `src/db.py:SCHEMA`; migrations are minimal

`init_db()` runs the full `CREATE TABLE IF NOT EXISTS` script, then `_migrate()` for any post-hoc column adds (currently `alerts.message`). When adding columns, append to `_migrate()` with a column-existence check — don't rewrite SCHEMA assuming the user re-creates the DB.

Retention: `daily_summary` and `alerts` are kept forever (one row per day, tiny). `activities` and `meals` are pruned by `prune_old_data()` after `--days` (default 90). The prune timer runs Sundays 03:00.

### Calorie balance combines four sources

`db.calorie_balance_for_date()` sums: meal `kcal` (eaten) vs activity `calories` + BMR (Mifflin-St Jeor from `cfg.user_*`) + step calories (~0.048 kcal/step scaled to weight). Requires the biometric env vars; without them BMR/steps fall to 0 and only activities count.

### Config is a frozen dataclass loaded once

`Config.from_env()` is the only loader; pass the resulting object everywhere. `dotenv` is loaded at import time in `src/config.py`.

### Dashboard: HTMX on the server, JSON on the client

`src/dashboard.py:create_app` exposes both:
- `/partials/all` — server-rendered HTML for HTMX swap-in (used for SSR-on-load)
- `/api/*` — JSON endpoints (`summary`, `trends`, `activities`, `alerts`, `readiness`, `meals`, `calorie-balance`) consumed by Chart.js

All frontend assets (Tailwind, Chart.js, htmx) come from CDN — no build step.

## systemd units

Three units, all in `systemd/`:

| Unit | Type | Cadence |
|---|---|---|
| `garmin-poller.{service,timer}` | oneshot | every 15 min |
| `garmin-digest.{service,timer}` | oneshot | daily 08:00 |
| `garmin-prune.{service,timer}` | oneshot | Sundays 03:00 |

Paths are hard-coded to `/home/pi/garmin-monitor`; edit `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, `User` if installing under a different user.

## Conventions

- **Python 3.11+**; std-lib first; type hints on public functions.
- **Sync only.** The poller and CLI are invoked by systemd timers (oneshot) and exit. Don't introduce asyncio.
- **Logging:** `logging` module, INFO default, key=value substrings where useful. The poller and smart-alert checks must never crash the process — they log and continue.
- **Secrets:** never commit `.env`, the cached token dir (`~/.garminconnect/`), or the SQLite DB. All secrets via env vars.
- **DB writes:** use the helpers in `src/db.py`. They open/close the connection per call and run with `WAL`+`synchronous=NORMAL`.
- **Garmin rate limits:** never poll more than once per minute. Cloudflare bans propagate fast.
- **Telegram:** `parse_mode=Markdown`. If you add reserved Markdown chars to alert text, they'll need escaping.

## Gotchas

- **`.env.example` is missing the biometric vars** (`USER_AGE`, `USER_HEIGHT_CM`, `USER_WEIGHT_KG`, `USER_SEX`). They're read by `Config.from_env()` with defaults, so the calorie math will use 30y/175cm/75kg/male unless the operator sets them.
- **First Garmin login may need MFA.** The poller prompts on stdin via `_prompt_mfa()`, which only works in an interactive TTY. For headless setup, run `python auth_setup.py` once interactively to cache tokens.
- **TLS fingerprinting:** `python-garminconnect` uses `curl_cffi` to mimic a real browser. If logins start failing, `pip install --upgrade garminconnect curl_cffi` first.
- **Daily-readiness checks need ~7 days of `daily_summary` history** before they fire — see `*_MIN_BASELINE_SAMPLES`.
- **DB cleanup after upgrading from a BLE-era install.** This refactor dropped the `hr_realtime` and `hrv` tables. `init-db` won't drop them on existing DBs (`CREATE TABLE IF NOT EXISTS`); the operator can run `sqlite3 garmin.db 'DROP TABLE IF EXISTS hr_realtime; DROP TABLE IF EXISTS hrv; VACUUM;'` once if they want the disk reclaimed.

## References

- `python-garminconnect`: https://github.com/cyberjunky/python-garminconnect
- Telegram Bot API: https://core.telegram.org/bots/api
- Open Food Facts (barcode lookup): https://world.openfoodfacts.org/data
