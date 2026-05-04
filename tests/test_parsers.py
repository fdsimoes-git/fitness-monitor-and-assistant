"""Unit tests for the BLE Heart Rate Measurement parser."""
from src.ble_listener import parse_hr_measurement


def test_uint8_no_rr():
    # flags=0x00 (uint8 HR, no RR), bpm=72
    bpm, rr = parse_hr_measurement(bytearray([0x00, 72]))
    assert bpm == 72
    assert rr == []


def test_uint16_hr():
    # flags=0x01 (uint16 HR), bpm=300 (0x012C)
    bpm, rr = parse_hr_measurement(bytearray([0x01, 0x2C, 0x01]))
    assert bpm == 300
    assert rr == []


def test_with_rr():
    # flags=0x10 (uint8 HR, RR present), bpm=60, two RR values
    # RR raw = 1024 -> 1000 ms ; RR raw = 768 -> 750 ms
    data = bytearray([0x10, 60, 0x00, 0x04, 0x00, 0x03])
    bpm, rr = parse_hr_measurement(data)
    assert bpm == 60
    assert rr == [1000, 750]


def test_with_energy_and_rr():
    # flags=0x18 (uint8 HR, energy expended + RR), bpm=80,
    # energy=200 (0x00C8), RR=512 raw -> 500 ms
    data = bytearray([0x18, 80, 0xC8, 0x00, 0x00, 0x02])
    bpm, rr = parse_hr_measurement(data)
    assert bpm == 80
    assert rr == [500]
