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

### Time convention: UTC for storage, Pi-local for day-bucketing

Two clocks coexist deliberately:

- **Storage timestamps** (`*.ts`, `meals.meal_time`, `daily_summary.fetched_at`, `alerts.ts`, etc.) are written as ISO-8601 UTC strings — they record the absolute moment something happened and must never lie about that.
- **Day-bucketing** (`daily_summary.date`, `activities.date`, the chat assistant's "today", any `WHERE date = …` query) follows the **Pi's local timezone**. Most date columns are stored as local YYYY-MM-DD strings (e.g. `daily_summary.date` is `date.today().isoformat()` from the poller; `activities.date` is `Garmin.startTimeLocal[:10]`).

The mismatch breaks near midnight UTC: ask `/today` at 22:30 local UTC-3 (= 01:30 UTC next day), and a UTC-anchored query returns "tomorrow"'s row while the user means today. Use `db.local_today()` for any "what local day is it?" calculation; reserve `datetime.now(timezone.utc)` for full timestamps. The bot, the chat system prompt, the chat tool dispatcher, and four `db.py` helpers (`recent_activities`, `prune_old_data`'s activities cutoff, `recent_calorie_balance`, `daily_readiness_history`) all route through this single seam — `tests/test_nutrition.py::test_recent_calorie_balance_anchors_on_local_today` (and friends) regression-test the convention.

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

### Telegram fitness/nutrition assistant

`src/telegram_bot.py` is a **chat-first agent**, modelled directly on asset-management's financial advisor. Plain text and photos default to `llm.chat()` with a 12-tool surface; the model orchestrates everything. A handful of fast-path commands bypass Claude for deterministic shortcuts:

- **`/help`, `/start`** → static help text.
- **`/today`, `/balance`** → `db.calorie_balance_for_date` formatted reply (no Claude call).
- **Numeric `^\d{8,14}$`** → `food.lookup_barcode` → `food.meal_from_barcode_info` → propose insert (no Claude call).
- **Anything else (text, photo, photo+caption)** → `llm.chat(cfg, text, pending, image_bytes=…)`.

The 12 tools registered in `llm.CHAT_TOOLS`:

| Read-only DB (8) | Validate-and-stash writes (3) | OFF lookup (1, read-only) |
|---|---|---|
| `get_balance` | `log_meal` (proposes insert) | `lookup_barcode` |
| `get_meals` | `edit_meal` (proposes partial update) | |
| `get_recent_meals` | `delete_meal` (proposes delete) | |
| `get_daily_summary` | | |
| `get_trends` | | |
| `get_activities` | | |
| `get_readiness` | | |
| `get_training_intel` | | |

`lookup_barcode` is read-only — it queries Open Food Facts and returns scaled nutrition. The model typically chains it into a `log_meal` proposal afterward; that's the only path that touches the DB.

#### Validate-and-stash two-phase write pattern

Mirrors `asset-management/server.js:4803-4823`. When Claude calls a write tool, the dispatcher in `llm._execute_chat_tool` does NOT touch the DB. It validates, builds a proposal entry — `{action: 'insert'|'edit'|'delete', meal | meal_id | fields | before, ts}` — and parks it in the per-process `pending` dict via `_stash_pending`. The tool result returned to Claude is `{pending_id, needs_confirmation: True, action, summary}`.

The bot's `_run_chat` snapshots `pending.keys()` before the call and diffs after. For each new pending entry it calls `_surface_pending`, which renders an action-aware Confirm/Cancel inline keyboard (✅ Log it / ✏️ Apply edit / 🗑 Delete). The `callback_data` shape (`confirm:UUID` / `cancel:UUID`) is uniform — `_handle_callback` doesn't need to know about action types; it pops the entry and dispatches to `_apply_pending`, which branches on `entry["action"]`:

- `insert` → `db.insert_meal` (`meal_time` normalized to UTC ISO via `_iso_to_utc_aware`)
- `edit` → `db.update_meal` (filters fields to `db.EDITABLE_MEAL_COLUMNS`; rejects None/empty for NOT NULL columns; normalizes any supplied `meal_time`)
- `delete` → `db.delete_meal`

Pending entries TTL-out after 1h via `_sweep_pending`. If the bot restarts mid-confirmation the entry is lost; the user just re-asks (which now hits the chat path again).

#### Vision

`llm.chat()` accepts optional `image_bytes` + `mime_type` and inserts them as a base64 image content block in the user message. The model sees the image alongside any caption text and can call any tool. Asset-management has no vision in chat — this is a Telegram-specific extension. **Image bytes are never persisted** — the buffer is dropped right after the SDK call returns; `tests/test_telegram_bot.py::test_photo_flow_does_not_leak_image_bytes_to_db` enforces this.

#### Live progress feedback

`llm.chat()` accepts an optional `progress_cb(tool_name, tool_input)` invoked just before each tool runs. The bot's `_run_chat`:

1. Sends a typing indicator and a "🔄 thinking…" status message at the start (capturing its `message_id`).
2. Builds a closure over that `message_id` that calls `_edit_status` with the per-tool label from `_TOOL_STATUS_LABELS` (e.g. "🏷️ looking up barcode 5449000000996…", "📝 preparing meal log…").
3. Passes that closure into `chat()` as `progress_cb`. Failures inside the callback are caught in `chat()` and logged — a broken UI hook never breaks the loop.
4. After `chat()` returns, edits the **same** status message into the final answer (single round-trip) instead of sending a new message. If only proposals were created (model went silent + Confirm/Cancel keyboards visible), deletes the status message.

Tests stub all four bot HTTP helpers (`_send_typing`, `_send_status`, `_edit_status`, `_delete_message`, plus `_answer_callback` and `_edit_message_remove_keyboard`) via an autouse fixture so the suite never touches `api.telegram.org`.

#### Conversational history

`run()` keeps a `history: list[{role, content}]` next to `pending` and threads it through `dispatch` → `_handle_text` / `_handle_photo` → `_run_chat`. Each chat-routed turn appends a `(user, assistant)` pair via `_append_history`; the list is trimmed to the last `HISTORY_MAX_PAIRS = 10` pairs (20 entries). `llm.chat()` prepends `history` verbatim to its `messages` list before the current user turn — it never mutates the caller's list.

Two design choices worth knowing:

- **Photo turns store placeholders**, not bytes. After a photo turn, history records `{"role": "user", "content": "[photo] caption_text"}` instead of the original list-with-image-block. This avoids re-sending old image bytes (token-cost) and prevents arbitrary growth in history payload size. Past photos are referenced by their effects (the assistant's prior reply describes what was logged).
- **Fast-path commands skip history.** `/help`, `/today`, `/balance`, and the numeric-barcode shortcut never touch the list. Only chat-routed messages contribute, so `/help` doesn't flush meaningful context.

History is per-process and lost on restart (matches `pending`'s lifetime). For a single-user Pi bot this is fine; if conversations need to survive restarts, persist to SQLite as a follow-up.

`src/llm.py` mirrors the credential-resolution pattern from `asset-management/server.js` (lines 241–302): if `CLAUDE_CODE_OAUTH_TOKEN` is set we use the OAuth route (Bearer token + `anthropic-beta: oauth-2025-04-20` header + a system prefix identifying the request as Claude Code) so calls bill to the user's Claude Code subscription; otherwise we use the standard `ANTHROPIC_API_KEY` route. Default model `claude-sonnet-4-6`; override via `CLAUDE_MODEL`.

**Photo bytes are never persisted.** The byte buffer is passed to Claude in-memory and dropped immediately; nothing is written to disk or SQLite. `tests/test_telegram_bot.py::test_photo_flow_does_not_leak_image_bytes_to_db` enforces this.

## systemd units

Four units, all in `systemd/`:

| Unit | Type | Cadence |
|---|---|---|
| `garmin-poller.{service,timer}` | oneshot | every 15 min |
| `garmin-digest.{service,timer}` | oneshot | daily 08:00 |
| `garmin-prune.{service,timer}` | oneshot | Sundays 03:00 |
| `garmin-bot.service` | simple, `Restart=on-failure` | always-on (optional) |

Paths are hard-coded to `/home/pi/garmin-monitor`; edit `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, `User` if installing under a different user.

## Conventions

- **Python 3.11+**; std-lib first; type hints on public functions.
- **Sync only.** The poller and CLI are invoked by systemd timers (oneshot) and exit. Don't introduce asyncio.
- **Logging:** `logging` module, INFO default, key=value substrings where useful. The poller and smart-alert checks must never crash the process — they log and continue.
- **Secrets:** never commit `.env`, the cached token dir (`~/.garminconnect/`), or the SQLite DB. All secrets via env vars.
- **DB writes:** use the helpers in `src/db.py`. They open/close the connection per call and run with `WAL`+`synchronous=NORMAL`.
- **Garmin rate limits:** never poll more than once per minute. Cloudflare bans propagate fast.
- **Telegram:** `parse_mode=Markdown` for static text (alerts, help). Dynamic replies that include free-text descriptions (logged meals, balance summaries with user-supplied food names) go through `_send_plain` with no parse_mode so an asterisk in `"M&M's"` doesn't break the message.
- **Anthropic OAuth code path** MUST send both the bearer token AND the `anthropic-beta: oauth-2025-04-20` header AND inject the "You are Claude Code, Anthropic's official CLI for Claude." system prefix as the first text block. All three are required together — drop any one and Sonnet/Opus calls fail with a misleading "credit balance" error rather than a proper auth failure. See `src/llm.py:build_anthropic_client` and `build_system_prompt`.
- **Raw input persistence is forbidden.** Photos, audio, etc. are passed to Claude in-memory and discarded. Don't add hash caches, blob columns, or `raw_json` payloads that include base64 image data.

## Gotchas

- **Biometric defaults are 30y/175cm/75kg/male** if the operator skips `USER_AGE` / `USER_HEIGHT_CM` / `USER_WEIGHT_KG` / `USER_SEX` in `.env`. Calorie math (BMR, step kcal, RED-S energy availability) is silently wrong but not broken.
- **First Garmin login may need MFA.** The poller prompts on stdin via `_prompt_mfa()`, which only works in an interactive TTY. For headless setup, run `python auth_setup.py` once interactively to cache tokens.
- **TLS fingerprinting:** `python-garminconnect` uses `curl_cffi` to mimic a real browser. If logins start failing, `pip install --upgrade garminconnect curl_cffi` first.
- **Daily-readiness checks need ~7 days of `daily_summary` history** before they fire — see `*_MIN_BASELINE_SAMPLES`.
- **DB cleanup after upgrading from a BLE-era install.** This refactor dropped the `hr_realtime` and `hrv` tables. `init-db` won't drop them on existing DBs (`CREATE TABLE IF NOT EXISTS`); the operator can run `sqlite3 garmin.db 'DROP TABLE IF EXISTS hr_realtime; DROP TABLE IF EXISTS hrv; VACUUM;'` once if they want the disk reclaimed.
- **Bot needs `requirements-bot.txt` installed.** The base install is intentionally Anthropic-free; `build_anthropic_client` raises a clear `RuntimeError` ("anthropic SDK is not installed. … pip install -r requirements-bot.txt") if the SDK isn't on the path. `tests/test_llm.py` uses `pytest.importorskip("anthropic")` so the test suite still runs cleanly on a base install — bot tests are skipped, the rest pass.
- **Misleading "credit balance" error** — see the Conventions entry. Rule of thumb: Haiku 4.5 succeeds without the prefix and was historically how this bug got missed; if Haiku works but Sonnet/Opus fails on the same OAuth token, suspect a missing system prefix.

## References

- `python-garminconnect`: https://github.com/cyberjunky/python-garminconnect
- Telegram Bot API: https://core.telegram.org/bots/api
- Open Food Facts (barcode lookup): https://world.openfoodfacts.org/data
- Anthropic Vision: https://docs.anthropic.com/claude/docs/vision
- Anthropic Tool Use: https://docs.anthropic.com/claude/docs/tool-use
