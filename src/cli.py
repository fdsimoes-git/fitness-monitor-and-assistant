"""CLI entry: python -m src.cli <subcommand>.

Imports for heavy/optional dependencies are deferred into each subcommand so
operators can run e.g. `init-db` without having `bleak` or `garminconnect`
installed yet.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, setup_logging

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="garmin-monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create tables in SQLite")
    sub.add_parser("ble", help="Run the BLE HR listener (blocks)")
    sub.add_parser("poll", help="Run one Garmin Connect poll")
    sub.add_parser("test-alert", help="Send a Telegram test message")
    digest_p = sub.add_parser("digest", help="Send the daily Telegram digest")
    digest_p.add_argument(
        "--date",
        help="ISO date (YYYY-MM-DD) to summarize. Defaults to yesterday.",
    )
    dash_p = sub.add_parser("dashboard", help="Run the FastAPI dashboard (optional)")
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8000)
    prune_p = sub.add_parser("prune", help="Delete old hr_realtime/hrv/activities rows and VACUUM")
    prune_p.add_argument("--days", type=int, default=90, help="Retention window in days (default 90)")
    act_p = sub.add_parser("activities", help="Print recent activities from the DB")
    act_p.add_argument("--days", type=int, default=14, help="Window in days (default 14)")

    args = p.parse_args(argv)
    cfg = Config.from_env()
    setup_logging(cfg.log_level)

    if args.cmd == "init-db":
        from . import db
        db.init_db(cfg.db_path)
        print(f"DB initialised at {cfg.db_path}")
        return 0

    if args.cmd == "test-alert":
        from . import alerts
        ok = alerts.test_alert(cfg)
        print("Sent" if ok else "Failed (check logs)")
        return 0 if ok else 1

    if args.cmd == "ble":
        import asyncio
        from . import ble_listener
        try:
            asyncio.run(ble_listener.run(cfg))
        except KeyboardInterrupt:
            log.info("Stopped by user")
        return 0

    if args.cmd == "poll":
        from . import garmin_poller
        garmin_poller.run_once(cfg)
        return 0

    if args.cmd == "digest":
        from datetime import date as date_cls
        from . import digest
        target = date_cls.fromisoformat(args.date) if args.date else None
        ok = digest.send_digest(cfg, target)
        print("Sent" if ok else "No data / send failed")
        return 0 if ok else 1

    if args.cmd == "dashboard":
        from . import dashboard
        dashboard.serve(cfg, host=args.host, port=args.port)
        return 0

    if args.cmd == "prune":
        from . import db
        with db.connect(cfg.db_path) as conn:
            deleted = db.prune_old_data(conn, days=args.days)
        print(
            f"Pruned (>{args.days}d): hr_realtime={deleted['hr_realtime']} "
            f"hrv={deleted['hrv']} activities={deleted['activities']}"
        )
        return 0

    if args.cmd == "activities":
        from . import db
        rows = db.recent_activities(cfg.db_path, days=args.days)
        if not rows:
            print(f"No activities in the last {args.days} days.")
            return 0
        for r in rows:
            dur = r.get("duration_s") or 0
            mins = dur // 60
            dist_km = (r["distance_m"] / 1000.0) if r.get("distance_m") else None
            dist_str = f"{dist_km:.2f}km" if dist_km else "—"
            avg = r.get("avg_hr") or "—"
            cals = r.get("calories") or "—"
            te = r.get("training_effect")
            te_str = f"TE {te:.1f}" if te is not None else "TE —"
            print(
                f"{r['date']}  {(r.get('activity_type') or '?'):<18}"
                f"  {(r.get('name') or ''):<28}"
                f"  {mins:>4}min  {dist_str:>8}  HR {avg:>3}  {cals:>4}kcal  {te_str}"
            )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
