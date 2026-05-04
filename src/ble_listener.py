"""Real-time BLE heart rate listener.

Subscribes to the standard BLE Heart Rate Measurement characteristic (0x2A37)
on any device advertising the Heart Rate Service (0x180D). Parses HR + RR
intervals per the Bluetooth GATT spec, computes rolling-window HRV (RMSSD),
and auto-reconnects on disconnect / out-of-range with exponential backoff.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import alerts, db
from .config import Config
from .hrv import HrvWindow
from .parsers import parse_hr_measurement  # re-exported for callers/tests

log = logging.getLogger(__name__)

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

SCAN_TIMEOUT_S = 15.0
INITIAL_BACKOFF_S = 2.0
MAX_BACKOFF_S = 300.0  # 5 min cap — watch may be off-wrist for a while


async def find_watch(timeout: float = SCAN_TIMEOUT_S):
    """Scan for any device advertising the HR service."""
    log.info("Scanning for BLE HR broadcaster (%.0fs)...", timeout)
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: HR_SERVICE_UUID in (ad.service_uuids or []),
        timeout=timeout,
    )
    if device is None:
        raise RuntimeError("No BLE HR device found. Is the watch broadcasting?")
    log.info("Found %s (%s)", device.name or "unnamed", device.address)
    return device


def make_handler(cfg: Config, hrv_window: HrvWindow) -> Callable:
    def on_notify(_, data: bytearray) -> None:
        try:
            bpm, rr = parse_hr_measurement(data)
        except Exception as e:
            log.warning("HR parse error: %s (data=%s)", e, data.hex())
            return

        log.debug("HR=%d bpm  RR=%s", bpm, rr)
        db.insert_hr(cfg.db_path, bpm, rr or None)

        if rr:
            sample = hrv_window.add(rr)
            if sample is not None:
                db.insert_hrv(cfg.db_path, sample.rmssd_ms, sample.rr_count, source="ble")
                log.info("HRV window: rmssd=%.1f ms (n=%d RR)",
                         sample.rmssd_ms, sample.rr_count)

        if bpm >= cfg.hr_high_bpm:
            alerts.maybe_alert(
                cfg,
                kind="hr_spike",
                text=f"⚠️ HR spike: *{bpm}* bpm (threshold {cfg.hr_high_bpm})",
                payload={"bpm": bpm, "rr": rr},
            )

    return on_notify


async def _run_one_session(cfg: Config, hrv_window: HrvWindow) -> None:
    """Single connect+stream cycle. Raises on any failure so the outer loop can retry."""
    device = await find_watch()
    async with BleakClient(device) as client:
        log.info("Connected to %s", device.address)
        await client.start_notify(HR_MEASUREMENT_UUID, make_handler(cfg, hrv_window))
        while client.is_connected:
            await asyncio.sleep(1.0)
        log.warning("BLE client reports disconnected")


async def run(cfg: Config) -> None:
    """Connect, subscribe, and stream forever.

    Wraps a single BLE session in a retry loop with exponential backoff. The
    watch routinely goes out of range or stops broadcasting (e.g. when the user
    starts an activity). Any exception is treated as recoverable; only
    asyncio.CancelledError exits the loop.
    """
    backoff = INITIAL_BACKOFF_S
    hrv_window = HrvWindow()  # persists across reconnects so windows aren't reset
    while True:
        try:
            await _run_one_session(cfg, hrv_window)
            # Clean disconnect — try again right away with reset backoff.
            backoff = INITIAL_BACKOFF_S
        except asyncio.CancelledError:
            log.info("BLE listener cancelled, exiting")
            raise
        except (BleakError, RuntimeError, OSError) as e:
            log.warning("BLE session ended: %s. Reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, MAX_BACKOFF_S)
        except Exception as e:  # last-resort guard so the daemon never dies
            log.exception("Unexpected BLE loop error: %s. Retrying in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, MAX_BACKOFF_S)
