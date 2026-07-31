"""Geometry tests for the dashboard's hand-rolled SVG (ACWR gauge, rings).

Pure-function tests — src.dashboard imports without FastAPI installed
(FastAPI is only needed inside create_app), so these run on a base install.
"""
import math
import re

from src import db
from src.dashboard import (
    _ACWR_BANDS,
    _ACWR_GAUGE_MAX,
    _GAUGE_CX,
    _GAUGE_CY,
    _GAUGE_R,
    _acwr_gauge_svg,
    _gauge_arc_path,
    _gauge_point,
    _hero_html,
)

_COORD_PAIR = re.compile(r"(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)")


def _band_paths() -> list[str]:
    return [
        _gauge_arc_path(lo / _ACWR_GAUGE_MAX, hi / _ACWR_GAUGE_MAX)
        for lo, hi, _color, _opacity in _ACWR_BANDS
    ]


def _dist_from_center(x: float, y: float) -> float:
    return math.hypot(x - _GAUGE_CX, y - _GAUGE_CY)


def test_gauge_point_endpoints():
    for pct, expected in [(0.0, (20.0, 100.0)), (0.5, (100.0, 20.0)), (1.0, (180.0, 100.0))]:
        x, y = _gauge_point(pct)
        assert math.isclose(x, expected[0], abs_tol=1e-6)
        assert math.isclose(y, expected[1], abs_tol=1e-6)


def test_gauge_points_lie_on_circle():
    for i in range(21):
        x, y = _gauge_point(i / 20)
        assert math.isclose(_dist_from_center(x, y), _GAUGE_R, abs_tol=1e-6)


def test_acwr_band_arcs_are_contiguous():
    """Band N must start (M x y) exactly where band N-1's arc ends — the
    original hand-coded paths broke this, putting each band on its own circle."""
    paths = _band_paths()
    assert paths[0].startswith("M 20.0 100.0 ")
    assert paths[-1].endswith(" 180.0 100.0")
    for prev, nxt in zip(paths, paths[1:]):
        prev_end = " ".join(prev.split()[-2:])
        nxt_start = " ".join(nxt.split()[1:3])
        assert prev_end == nxt_start


def test_acwr_band_edges_match_db_thresholds():
    """Junctions between bands must land at the ratios db.acwr classifies on."""
    paths = _band_paths()
    thresholds = (db.ACWR_UNDERTRAINED_MAX, db.ACWR_OPTIMAL_MAX, db.ACWR_CAUTION_MAX)
    for path, threshold in zip(paths[1:], thresholds):
        mx, my = (float(v) for v in path.split()[1:3])
        ex, ey = _gauge_point(threshold / _ACWR_GAUGE_MAX)
        assert math.isclose(mx, round(ex, 1), abs_tol=1e-9)
        assert math.isclose(my, round(ey, 1), abs_tol=1e-9)


def test_acwr_band_endpoints_on_circle():
    """Regression: original breakpoints sat at distance 82.4/84.4 from center."""
    for path in _band_paths():
        for sx, sy in _COORD_PAIR.findall(path.replace("A 80 80 0 0 1", "")):
            assert math.isclose(_dist_from_center(float(sx), float(sy)), _GAUGE_R, abs_tol=0.05)


def test_acwr_gauge_needle_tip_on_ring():
    svg = _acwr_gauge_svg(1.0, "optimal")
    m = re.search(r'x2="(-?[\d.]+)" y2="(-?[\d.]+)"', svg)
    assert m is not None
    assert math.isclose(_dist_from_center(float(m.group(1)), float(m.group(2))), _GAUGE_R, abs_tol=0.1)

    svg_empty = _acwr_gauge_svg(None, None)
    m = re.search(r'x2="(-?[\d.]+)" y2="(-?[\d.]+)"', svg_empty)
    assert m is not None
    assert math.isclose(float(m.group(1)), 100.0, abs_tol=0.1)
    assert math.isclose(float(m.group(2)), 20.0, abs_tol=0.1)
    assert "AWAITING DATA" in svg_empty


def test_hero_readiness_pct_clamped():
    over = _hero_html({"score": 120, "band": "high", "components": {}}, {})
    assert 'data-ring-pct="1.0000"' in over
    empty = _hero_html({"score": None, "band": None, "components": {}}, {})
    assert 'data-ring-pct="0.0000"' in empty
