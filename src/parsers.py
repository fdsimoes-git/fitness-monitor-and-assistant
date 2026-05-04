"""Pure parsers for BLE GATT payloads.

Kept separate from `ble_listener` so unit tests don't need the `bleak`
dependency to run.
"""
from __future__ import annotations


def parse_hr_measurement(data: bytearray) -> tuple[int, list[int]]:
    """Parse the Heart Rate Measurement characteristic (0x2A37).

    Returns (bpm, rr_intervals_ms). RR list may be empty.
    Spec: https://www.bluetooth.com/specifications/specs/heart-rate-service-1-0/
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
        # RR values are 1/1024 second resolution, uint16 little-endian.
        while offset + 1 < len(data):
            raw = int.from_bytes(data[offset:offset + 2], "little")
            rr_intervals.append(round(raw * 1000 / 1024))
            offset += 2

    return bpm, rr_intervals
