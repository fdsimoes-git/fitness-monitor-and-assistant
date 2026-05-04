"""Unit tests for the HRV rolling-window RMSSD computation."""
import math

from src.hrv import HrvWindow, rmssd


def test_rmssd_basic():
    # diffs = [10, 10, 10] -> rmssd = 10
    assert math.isclose(rmssd([100, 110, 120, 130]), 10.0)


def test_rmssd_zero_when_constant():
    assert rmssd([800, 800, 800, 800]) == 0.0


def test_rmssd_too_few_samples():
    assert rmssd([800]) == 0.0
    assert rmssd([]) == 0.0


def test_rmssd_known_values():
    # diffs = [50, -50, 50] ; squared = [2500, 2500, 2500] ; mean = 2500 ; sqrt = 50
    assert math.isclose(rmssd([800, 850, 800, 850]), 50.0)


def test_window_emits_after_enough_data():
    w = HrvWindow(window_seconds=300, emit_interval_seconds=10, min_rr=20)
    # Feed 30 RR intervals of ~1000ms (30s of data, more than 10s emit interval).
    sample = None
    for _ in range(30):
        sample = w.add([1000]) or sample
    assert sample is not None
    assert sample.rr_count == 30
    # All-equal RRs => rmssd == 0
    assert sample.rmssd_ms == 0.0


def test_window_does_not_emit_with_too_few_rr():
    w = HrvWindow(window_seconds=300, emit_interval_seconds=1, min_rr=20)
    # Only 5 RRs — should not emit even though emit interval has elapsed.
    out = w.add([1000, 1000, 1000, 1000, 1000])
    assert out is None


def test_window_trims_old_data():
    w = HrvWindow(window_seconds=10, emit_interval_seconds=1, min_rr=2)
    # Push 15 seconds of 1000ms RRs — buffer should hold at most ~10 seconds.
    for _ in range(15):
        w.add([1000])
    # Buffer must not exceed window_ms in cumulative duration.
    assert w._buf_sum_ms < 10_000 + 1000  # allow one trailing sample
