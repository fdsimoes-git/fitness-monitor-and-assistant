# garmin-monitor

Personal Garmin health platform for Raspberry Pi 5. The Garmin Connect poller pulls daily summary metrics (HR, steps, sleep, stress, HRV, activities), nutrition is logged via barcode + manual entry, smart alerts trigger over Telegram, and a FastAPI dashboard renders everything with a few metrics Garmin doesn't surface raw (composite readiness, ACWR, Z2 minutes, training monotony, sleep debt).

Storage is one local SQLite DB. No cloud, no third party between the Pi and Garmin Connect except `python-garminconnect`.

## Quick start (Raspberry Pi 5, Pi OS Bookworm)

```bash
git clone <your-repo-url> garmin-monitor
cd garmin-monitor
bash scripts/setup.sh
cp .env.example .env
# edit .env with your Garmin credentials, Telegram bot token, chat id, biometrics
.venv/bin/python -m src.cli init-db
.venv/bin/python auth_setup.py     # cache Garmin tokens once (handles MFA)
.venv/bin/python -m src.cli poll   # first poll
```

## Telegram bot setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → save the token
2. Message your new bot anything (so it can reply to you)
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your `chat.id`
4. Drop both into `.env`

## Run as a service (auto-start on boot)

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now garmin-poller.timer    # poll every 15 min
sudo systemctl enable --now garmin-digest.timer    # daily 08:00 Telegram digest
sudo systemctl enable --now garmin-prune.timer     # weekly cleanup
```

## Architecture

```
┌──────────────┐  HTTPS    ┌──────────────────┐   ┌──────────┐   ┌──────────┐
│Garmin Connect│◄──────────│ garmin_poller.py │──►│ db.py    │──►│alerts.py │──► Telegram
└──────────────┘           └──────────────────┘   │ SQLite   │   └──────────┘
                                                  └──────┬───┘
                                                         │
                          Open Food Facts ──► food.py ──►┤
                                                         ▼
                                                  ┌──────────────┐
                                                  │ dashboard.py │ ──► browser
                                                  └──────────────┘
```

## Storage & retention

`activities` and `meals` are pruned after 90 days; `daily_summary` and `alerts` are kept forever (one row per day each, negligible size).

A weekly cron-style timer runs `prune_old_data` and `VACUUM`:

```bash
.venv/bin/python -m src.cli prune --days 60   # custom window
```

## What's done vs what's next

See [CLAUDE.md](./CLAUDE.md) — it's the source of truth for Claude Code (and humans) on conventions, current state, and the roadmap.

## License

MIT — personal use. Note that the Connect poller relies on `python-garminconnect`, which scrapes the consumer Garmin Connect site; this is unofficial and may break when Garmin updates their auth flow. For commercial use you'd need the [official Garmin Health API](https://developer.garmin.com/gc-developer-program/health-api/).
