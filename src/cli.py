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

    log_meal_p = sub.add_parser("log-meal", help="Log a meal manually")
    log_meal_p.add_argument("--desc", required=True, help="Food description")
    log_meal_p.add_argument("--kcal", type=float)
    log_meal_p.add_argument("--protein", type=float)
    log_meal_p.add_argument("--carbs", type=float)
    log_meal_p.add_argument("--fat", type=float)
    log_meal_p.add_argument("--meal-time", help="ISO timestamp (defaults to now)")

    log_bc_p = sub.add_parser("log-barcode", help="Lookup a barcode and log a meal")
    log_bc_p.add_argument("barcode", help="EAN/UPC barcode")
    log_bc_p.add_argument("--grams", type=float, help="Serving size in grams")
    log_bc_p.add_argument("--meal-time", help="ISO timestamp (defaults to now)")

    meals_p = sub.add_parser("meals", help="Show meals for a date with totals")
    meals_p.add_argument("--date", help="ISO date (YYYY-MM-DD); default today")

    bal_p = sub.add_parser("calorie-balance", help="Show eaten vs burned for a date")
    bal_p.add_argument("--date", help="ISO date (YYYY-MM-DD); default today")

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
            f"hrv={deleted['hrv']} activities={deleted['activities']} "
            f"meals={deleted['meals']}"
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

    if args.cmd == "log-meal":
        from . import db
        meal = {
            "description": args.desc,
            "source": "manual",
            "kcal": args.kcal,
            "protein_g": args.protein,
            "carbs_g": args.carbs,
            "fat_g": args.fat,
            "meal_time": args.meal_time,
        }
        mid = db.insert_meal(cfg.db_path, meal)
        print(f"Logged meal #{mid}: {args.desc} ({args.kcal or '—'} kcal)")
        return 0

    if args.cmd == "log-barcode":
        import json
        from . import db, food
        info = food.lookup_barcode(args.barcode)
        if info is None:
            print(f"Barcode {args.barcode} not found in Open Food Facts.")
            return 1
        grams = args.grams
        if grams is None:
            grams = info.get("serving_size_g") or 100.0
            print(f"No --grams given; using {grams}g (serving size from API).")
        factor = grams / 100.0

        def _scale(per100: float | None) -> float | None:
            return round(per100 * factor, 1) if per100 is not None else None

        meal = {
            "description": f"{info['name']} ({grams:.0f}g)",
            "source": "barcode",
            "barcode": args.barcode,
            "kcal": _scale(info.get("kcal_100g")),
            "protein_g": _scale(info.get("protein_100g")),
            "carbs_g": _scale(info.get("carbs_100g")),
            "fat_g": _scale(info.get("fat_100g")),
            "meal_time": args.meal_time,
            "raw_json": json.dumps(info.get("raw") or {}),
        }
        mid = db.insert_meal(cfg.db_path, meal)
        print(
            f"Logged meal #{mid}: {meal['description']} → "
            f"{meal['kcal'] or '—'} kcal, "
            f"P{meal['protein_g'] or '—'}/C{meal['carbs_g'] or '—'}/F{meal['fat_g'] or '—'}g"
        )
        return 0

    if args.cmd == "meals":
        from datetime import date as date_cls, datetime as dt_cls
        from . import db
        target = args.date or date_cls.today().isoformat()
        rows = db.meals_for_date(cfg.db_path, target)
        if not rows:
            print(f"No meals on {target}.")
            return 0
        print(f"Meals on {target}:")
        for r in rows:
            try:
                t = dt_cls.fromisoformat(r["meal_time"]).astimezone().strftime("%H:%M")
            except (TypeError, ValueError):
                t = (r.get("meal_time") or "")[11:16]
            kcal = r.get("kcal")
            kcal_str = f"{kcal:>5.0f}" if kcal is not None else "    —"
            print(f"  {t}  {kcal_str} kcal  {r.get('description')}")
        bal = db.calorie_balance_for_date(cfg.db_path, target)
        print(
            f"\nTotals: {bal['eaten_kcal']:.0f} kcal · "
            f"P{bal['protein_g']:.0f}g / C{bal['carbs_g']:.0f}g / F{bal['fat_g']:.0f}g"
        )
        return 0

    if args.cmd == "calorie-balance":
        from datetime import date as date_cls
        from . import db
        target = args.date or date_cls.today().isoformat()
        bal = db.calorie_balance_for_date(cfg.db_path, target)
        sign = "surplus" if bal["balance_kcal"] >= 0 else "deficit"
        print(
            f"Calorie balance — {target}\n"
            f"  Eaten:   {bal['eaten_kcal']:>6.0f} kcal ({bal['meal_count']} meals)\n"
            f"  Burned:  {bal['burned_kcal']:>6} kcal (activities)\n"
            f"  Balance: {bal['balance_kcal']:>+6.0f} kcal ({sign})\n"
            f"  Macros:  P{bal['protein_g']:.0f}g / C{bal['carbs_g']:.0f}g / F{bal['fat_g']:.0f}g"
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
