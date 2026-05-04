"""Rolling-window HRV (RMSSD) computation from RR intervals.

RMSSD = sqrt( mean( (RR[i+1] - RR[i])^2 ) ) over a 5-minute window.
A new sample is emitted at most once per WINDOW_SECONDS; older intervals are
trimmed by their cumulative RR sum (since RR intervals *are* the time axis).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

WINDOW_SECONDS = 300  # 5 minutes
EMIT_INTERVAL_SECONDS = 60  # emit at most one HRV sample per minute
MIN_RR_FOR_RMSSD = 20  # below this, RMSSD is too noisy to be useful


@dataclass(frozen=True)
class HrvSample:
    rmssd_ms: float
    rr_count: int


def rmssd(rr_ms: list[int]) -> float:
    """Root-mean-square of successive differences. Returns 0.0 if fewer than 2 samples."""
    if len(rr_ms) < 2:
        return 0.0
    diffs_sq = [(rr_ms[i + 1] - rr_ms[i]) ** 2 for i in range(len(rr_ms) - 1)]
    return math.sqrt(sum(diffs_sq) / len(diffs_sq))


class HrvWindow:
    """Maintains a rolling window of RR intervals and emits RMSSD samples.

    Call .add(rr_intervals_ms) for each batch of RR values arriving from the
    BLE notifications. Returns an HrvSample when a new measurement is ready,
    None otherwise.
    """

    def __init__(
        self,
        window_seconds: int = WINDOW_SECONDS,
        emit_interval_seconds: int = EMIT_INTERVAL_SECONDS,
        min_rr: int = MIN_RR_FOR_RMSSD,
    ) -> None:
        self._window_ms = window_seconds * 1000
        self._emit_interval_ms = emit_interval_seconds * 1000
        self._min_rr = min_rr
        self._buf: deque[int] = deque()
        self._buf_sum_ms = 0
        self._ms_since_last_emit = 0

    def add(self, rr_intervals_ms: list[int]) -> HrvSample | None:
        if not rr_intervals_ms:
            return None
        for rr in rr_intervals_ms:
            self._buf.append(rr)
            self._buf_sum_ms += rr
            self._ms_since_last_emit += rr

        # Trim window from the left while we have more than window_seconds of RR.
        while self._buf and self._buf_sum_ms - self._buf[0] >= self._window_ms:
            self._buf_sum_ms -= self._buf.popleft()

        if (
            self._ms_since_last_emit >= self._emit_interval_ms
            and len(self._buf) >= self._min_rr
        ):
            sample = HrvSample(rmssd_ms=rmssd(list(self._buf)), rr_count=len(self._buf))
            self._ms_since_last_emit = 0
            return sample
        return None
