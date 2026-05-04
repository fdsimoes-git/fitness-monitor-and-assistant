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

    return 2


if __name__ == "__main__":
    sys.exit(main())
