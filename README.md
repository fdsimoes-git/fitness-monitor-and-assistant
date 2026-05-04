# garmin-monitor

Personal Garmin health platform for Raspberry Pi 5. Combines two data sources into one local SQLite database:

1. **BLE listener** — Real-time heart rate from your Garmin watch's BLE broadcast (~1 Hz, sub-second latency)
2. **Garmin Connect poller** — Periodic pulls of daily summary metrics (steps, sleep, stress, HRV, etc.)

Both services push notifications to **Telegram** when configurable thresholds are crossed.

## Why two paths?

| Aspect | BLE listener | Connect poller |
|---|---|---|
| Latency | ~1 second | 5–15 minutes |
| Range | ~10 m | Anywhere |
| Data | HR + RR intervals | Everything Garmin computes |
| Reliable | While watch is broadcasting | Yes |
| Official | Standard BLE HRP | Unofficial scraping |

Run both. BLE during workouts and acute monitoring, poller for trend analysis.

## Quick start (Raspberry Pi 5, Pi OS Bookworm)

```bash
git clone <your-repo-url> garmin-monitor
cd garmin-monitor
bash scripts/setup.sh
cp .env.example .env
# edit .env with your Garmin credentials, Telegram bot token, chat id
.venv/bin/python -m src.cli init-db
.venv/bin/python -m src.cli poll  # one-shot test
.venv/bin/python -m src.cli ble   # live HR listener
```

## Telegram bot setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → save the token
2. Message your new bot anything (so it can reply to you)
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your `chat.id`
4. Drop both into `.env`

## Garmin watch setup (BLE)

Enable broadcast mode on the watch — exact path depends on the model. Common ones:

- **Forerunner / Fenix / Epix:** Hold UP → Settings → Sensors & Accessories → Wrist HR → Broadcast
- **Venu / Vivoactive:** Settings → Heart Rate → Broadcast HR
- **Older models:** Hold the light/menu button → Broadcast HR

The watch must stay within ~10 m of the Pi while broadcasting. Note that on most models the watch can't simultaneously record an activity.

## Run as a service (auto-start on boot)

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now garmin-poller.timer
sudo systemctl enable --now garmin-ble.service   # optional, only if always near the Pi
```

## Architecture

```
┌─────────────┐  BLE HRP    ┌──────────────────┐
│ Garmin Watch│────────────►│  ble_listener.py │──┐
└─────────────┘             └──────────────────┘  │
                                                  ▼
┌─────────────┐  HTTPS      ┌──────────────────┐ ┌──────────┐  ┌──────────┐
│Garmin Connect│◄───────────│ garmin_poller.py │►│ db.py    │►│alerts.py │──► Telegram
└─────────────┘             └──────────────────┘ │ SQLite   │  └──────────┘
                                                 └──────────┘
```

## What's done vs what's next

See [CLAUDE.md](./CLAUDE.md) — it's the source of truth for Claude Code (and humans) on conventions, current state, and the roadmap.

## License

MIT — personal use. Note that the Connect poller relies on `python-garminconnect`, which scrapes the consumer Garmin Connect site; this is unofficial and may break when Garmin updates their auth flow. For commercial use you'd need the [official Garmin Health API](https://developer.garmin.com/gc-developer-program/health-api/).
