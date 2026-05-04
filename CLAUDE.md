# CLAUDE.md

Context for Claude Code working in this repo. Read this before making changes.

## What this is

Personal Garmin health-data platform on a Raspberry Pi 5. Two data ingestion paths feed one SQLite database, with Telegram alerts on threshold crossings.

## Conventions

- **Language:** Python 3.11+ (Pi OS Bookworm default)
- **Style:** standard library first, type hints on public functions, `ruff` if added
- **Async:** `ble_listener` is asyncio. The poller is sync (cron/timer-driven, no need for async)
- **Secrets:** never commit `.env`, tokens, or `~/.garminconnect/`. All secrets via env vars.
- **Logging:** `logging` module, INFO by default, structured key=value where helpful
- **DB writes:** wrap multi-row writes in transactions; use the helpers in `src/db.py`

## Current state (scaffolded, not yet hardened)

| Component | Status | Notes |
|---|---|---|
| `src/config.py` | Working | env loading, paths |
| `src/db.py` | Working | schema + insert helpers |
| `src/alerts.py` | Working | basic Telegram + cooldown |
| `src/ble_listener.py` | Working skeleton | needs auto-reconnect, RR/HRV calc |
| `src/garmin_poller.py` | Working skeleton | needs full metric coverage |
| `src/cli.py` | Working | argparse subcommands |
| systemd units | Provided | tested only on paper |
| Tests | Stub | `tests/test_parsers.py` only |

## Roadmap (priority order)

1. **BLE auto-reconnect.** When the watch disappears (out of range, broadcast off), the listener currently exits. Wrap in a retry loop with exponential backoff.
2. **HRV from RR intervals.** The HR Measurement characteristic carries RR intervals (bit 4 of flags byte). Parse them, compute RMSSD over rolling 5-minute windows, store in a new `hrv` table.
3. **Sleep + stress poller coverage.** Currently only HR/steps. Add `client.get_sleep_data()`, `client.get_stress_data()`, `client.get_hrv_data()`.
4. **Smarter alerts.**
   - HR-while-resting detection (high HR when accelerometer/movement is low — though we don't have movement data, so use time-of-day heuristics)
   - Trend-based: "your resting HR has been climbing 5 days in a row"
   - Recovery: HRV drop > 20% from 7-day baseline
5. **Web dashboard.** Tiny FastAPI + HTMX page on the Pi serving charts from the SQLite DB. Optional.
6. **Daily digest.** Cron at 8am, summary message via Telegram.
7. **ANT+ option.** `openant` library + USB ANT+ stick — lets the watch record an activity AND broadcast simultaneously, which BLE HRP can't.

## Gotchas

- **Garmin Connect MFA:** First login may prompt for MFA. Tokens cache to `~/.garminconnect/`. After that it's headless.
- **TLS fingerprinting:** `python-garminconnect` uses `curl_cffi` to mimic a real browser. If logins start failing, `pip install --upgrade garminconnect curl_cffi` first.
- **BLE on Linux:** `bleak` uses BlueZ via D-Bus. If scans return nothing, check `bluetoothctl` works and `bluetooth.service` is running.
- **Pi 5 BLE:** Built-in. No dongle needed. Bluetooth 5.0.
- **Rate limits:** Don't poll Garmin more than once per minute. They will rate-limit and may temporarily ban.
- **Watch behavior:** Most Garmin watches stop broadcasting HR when an activity recording starts. ANT+ doesn't have this limitation.

## Useful references

- `python-garminconnect` API: https://github.com/cyberjunky/python-garminconnect
- BLE Heart Rate Service spec (UUID 0x180D, characteristic 0x2A37): https://www.bluetooth.com/specifications/specs/heart-rate-service-1-0/
- Telegram Bot API: https://core.telegram.org/bots/api
- `bleak` docs: https://bleak.readthedocs.io

## How to test changes locally (without a watch)

```bash
# DB + alerts work without any hardware
.venv/bin/python -m src.cli init-db
.venv/bin/python -m src.cli test-alert

# Poller needs real Garmin creds in .env
.venv/bin/python -m src.cli poll

# BLE listener needs a watch in broadcast mode within ~10m
.venv/bin/python -m src.cli ble
```
