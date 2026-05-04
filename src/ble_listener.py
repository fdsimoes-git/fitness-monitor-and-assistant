"""Real-time BLE heart rate listener.

Subscribes to the standard BLE Heart Rate Measurement characteristic (0x2A37)
on any device advertising the Heart Rate Service (0x180D). Parses HR + RR
intervals per the Bluetooth GATT spec.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from bleak import BleakClient, BleakScanner

from . import alerts, db
from .config import Config

log = logging.getLogger(__name__)

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def parse_hr_measurement(data: bytearray) -> tuple[int, list[int]]:
    """Parse the Heart Rate Measurement characteristic.

    Returns (bpm, rr_intervals_ms). RR list may be empty.
    See: https://www.bluetooth.com/specifications/specs/heart-rate-service-1-0/
    """
    flags = data[0]
    hr_uint16 = bool(flags & 0x01)
    rr_present = bool(flags & 0x10)
    energy_present = bool(flags & 0x08)

    offset = 1
    if hr_uint16:
        bpm = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2
    else:
        bpm = data[offset]
        offset += 1

    if energy_present:
        offset += 2  # skip energy expended

    rr_intervals: list[int] = []
    if rr_present:
        # RR values are 1/1024 second resolution, uint16 little-endian
        while offset + 1 < len(data):
            raw = int.from_bytes(data[offset:offset + 2], "little")
            rr_intervals.append(round(raw * 1000 / 1024))
            offset += 2

    return bpm, rr_intervals


async def find_watch(timeout: float = 15.0):
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


def make_handler(cfg: Config) -> Callable:
    def on_notify(_, data: bytearray) -> None:
        try:
            bpm, rr = parse_hr_measurement(data)
        except Exception as e:
            log.warning("HR parse error: %s (data=%s)", e, data.hex())
            return

        log.debug("HR=%d bpm  RR=%s", bpm, rr)
        db.insert_hr(cfg.db_path, bpm, rr or None)

        if bpm >= cfg.hr_high_bpm:
            alerts.maybe_alert(
                cfg,
                kind="hr_spike",
                text=f"⚠️ HR spike: *{bpm}* bpm (threshold {cfg.hr_high_bpm})",
                payload={"bpm": bpm, "rr": rr},
            )

    return on_notify


async def run(cfg: Config) -> None:
    """Connect, subscribe, and stream until cancelled. Auto-reconnects on disconnect."""
    backoff = 1.0
    while True:
        try:
            device = await find_watch()
            async with BleakClient(device) as client:
                log.info("Connected to %s", device.address)
                backoff = 1.0  # reset backoff on success
                await client.start_notify(HR_MEASUREMENT_UUID, make_handler(cfg))
                # Sleep forever; bleak will raise on disconnect
                while client.is_connected:
                    await asyncio.sleep(1.0)
                log.warning("BLE client disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("BLE loop error: %s. Retrying in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
