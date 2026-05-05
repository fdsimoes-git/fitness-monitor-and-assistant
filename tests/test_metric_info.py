"""Tests for the metric-info registry."""
import tempfile
from datetime import date
from pathlib import Path

import pytest

from src import db, metric_info
from src.config import Config


def _make_cfg(db_path: Path) -> Config:
    return Config(
        garmin_email="x", garmin_password="x", garmin_token_dir=Path("/tmp/x"),
        telegram_bot_token="x", telegram_chat_id="x",
        db_path=db_path,
        hr_resting_high_bpm=85, alert_cooldown_seconds=0, poll_interval_minutes=15,
        log_level="INFO",
        user_age=30, user_height_cm=175, user_weight_kg=75, user_sex="male",
        protein_target_g_per_kg=1.6, kcal_target=2200,
        sleep_target_hours=8, user_hrmax=0,
    )


@pytest.fixture
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        yield p


def test_every_registry_entry_has_required_fields():
    """Every metric must have title, what_html, build_insight, sources."""
    for mid, entry in metric_info.INFO.items():
        assert entry.get("title"), f"{mid}: missing title"
        assert entry.get("what_html"), f"{mid}: missing what_html"
        assert callable(entry.get("build_insight")), f"{mid}: missing build_insight"
        assert isinstance(entry.get("sources"), list), f"{mid}: sources must be a list"
        assert entry["sources"], f"{mid}: at least one source required"
        for src in entry["sources"]:
            assert "title" in src and "url" in src, f"{mid}: malformed source {src}"
            assert src["url"].startswith("http"), f"{mid}: bad URL {src['url']}"


def test_build_payload_returns_one_entry_per_metric(tmpdb):
    """build_payload must produce JSON-shaped data for every registered metric."""
    cfg = _make_cfg(tmpdb)
    payload = metric_info.build_payload({"cfg": cfg})
    assert set(payload.keys()) == set(metric_info.INFO.keys())
    for mid, item in payload.items():
        assert set(item.keys()) == {"title", "what", "insight", "sources"}, mid
        assert "<p>" in item["insight"], f"{mid}: insight should be HTML"


def test_insight_handles_empty_context_gracefully(tmpdb):
    """Insights must not crash when called with an effectively-empty context."""
    cfg = _make_cfg(tmpdb)
    payload = metric_info.build_payload({"cfg": cfg})
    # Each insight should fall back to the "not enough data" message rather than raise.
    for mid, item in payload.items():
        assert item["insight"], f"{mid}: empty insight html"


def test_readiness_insight_uses_score(tmpdb):
    cfg = _make_cfg(tmpdb)
    ctx = {"cfg": cfg, "readiness": {"score": 88, "band": "high",
                                      "components": {"hrv": 0.3, "rhr": 0.1, "sleep": 0.2, "bb": 0.4}}}
    out = metric_info.build_payload(ctx)["readiness"]["insight"]
    assert "88 / 100" in out
    assert "high" in out.lower()


def test_acwr_insight_describes_band(tmpdb):
    cfg = _make_cfg(tmpdb)
    ctx = {"cfg": cfg, "acwr": {"ratio": 1.7, "acute_avg": 600, "chronic_avg": 350, "band": "high_risk"}}
    out = metric_info.build_payload(ctx)["acwr"]["insight"]
    assert "1.70" in out
    assert "Gabbett" in out


def test_z2_insight_progress_messages(tmpdb):
    cfg = _make_cfg(tmpdb)
    ctx = {"cfg": cfg, "z2": {"minutes": 180, "goal_minutes": 150,
                              "lower_bpm": 114, "upper_bpm": 133, "hrmax": 190}}
    out = metric_info.build_payload(ctx)["z2"]["insight"]
    assert "150" in out


def test_calorie_balance_insight_classifies_state(tmpdb):
    cfg = _make_cfg(tmpdb)
    ctx = {"cfg": cfg, "balance": {"eaten_kcal": 2400, "balance_kcal": 600, "meal_count": 3}}
    out = metric_info.build_payload(ctx)["calorie_balance"]["insight"]
    assert "Surplus" in out or "surplus" in out
