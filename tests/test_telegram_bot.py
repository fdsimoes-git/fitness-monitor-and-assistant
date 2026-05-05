"""Tests for src/telegram_bot.py.

Every outbound HTTP call (Telegram, Anthropic, Open Food Facts) is mocked.
The tests poke `dispatch()` directly with synthetic Telegram update payloads
rather than spinning up the long-poll loop.
"""
from __future__ import annotations

import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import db, telegram_bot
from src.config import Config


@pytest.fixture
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        db.init_db(p)
        yield p


def _make_cfg(db_path: Path, **overrides) -> Config:
    base = dict(
        garmin_email="x", garmin_password="x", garmin_token_dir=Path("/tmp/x"),
        telegram_bot_token="bot-token", telegram_chat_id="42",
        db_path=db_path,
        hr_resting_high_bpm=85, alert_cooldown_seconds=0,
        poll_interval_minutes=15, log_level="INFO",
        user_age=30, user_height_cm=175, user_weight_kg=75, user_sex="male",
        protein_target_g_per_kg=1.6, kcal_target=2200,
        sleep_target_hours=8, user_hrmax=0,
        anthropic_api_key="sk-ant-api03-x",
        claude_oauth_token="",
        claude_model="claude-sonnet-4-6",
    )
    base.update(overrides)
    return Config(**base)


def _msg(text: str | None = None, photo: list[dict] | None = None, chat_id: int = 42) -> dict:
    """Build a Telegram message-update payload."""
    body: dict = {"update_id": 1, "message": {"chat": {"id": chat_id}, "message_id": 99}}
    if text is not None:
        body["message"]["text"] = text
    if photo is not None:
        body["message"]["photo"] = photo
    return body


def _callback(data: str, chat_id: int = 42) -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb-1",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": 99},
        },
    }


# ── whitelist ─────────────────────────────────────────────────────────────


def test_dispatch_rejects_other_chat_ids(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.alerts.send_telegram") as mock_send, \
         patch("src.telegram_bot.llm.extract_meal_from_text") as mock_llm:
        telegram_bot.dispatch(cfg, _msg(text="hello", chat_id=999), {})
    mock_send.assert_not_called()
    mock_llm.assert_not_called()


# ── command flow ──────────────────────────────────────────────────────────


def test_dispatch_handles_help_command(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.alerts.send_telegram") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="/help"), {})
    mock_send.assert_called_once()
    assert "garmin-monitor bot" in mock_send.call_args.args[1]


def test_dispatch_routes_ask_to_chat(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.llm.chat", return_value="Your protein is on track.") as mock_chat, \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="/ask How's my protein this week?"), {})
    mock_chat.assert_called_once_with(cfg, "How's my protein this week?")
    mock_send.assert_called_once()
    assert "protein is on track" in mock_send.call_args.args[1]


def test_dispatch_chat_alias_routes_to_chat(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.llm.chat", return_value="ok") as mock_chat, \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(cfg, _msg(text="/chat sleep tonight?"), {})
    mock_chat.assert_called_once_with(cfg, "sleep tonight?")


def test_dispatch_ask_without_body_shows_usage(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.llm.chat") as mock_chat, \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="/ask"), {})
    mock_chat.assert_not_called()
    mock_send.assert_called_once()
    assert "after the command" in mock_send.call_args.args[1].lower()


def test_dispatch_ask_handles_chat_returning_none(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.llm.chat", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="/ask broken question"), {})
    mock_send.assert_called_once()
    assert "couldn't reach Claude" in mock_send.call_args.args[1]


def test_dispatch_today_command_pulls_balance(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.alerts.send_telegram") as mock_send, \
         patch("src.telegram_bot.db.calorie_balance_for_date") as mock_bal:
        mock_bal.return_value = {
            "date": "2026-05-05", "eaten_kcal": 800.0, "burned_kcal": 0,
            "bmr_kcal": 1700, "steps_burned_kcal": 250, "total_burned_kcal": 1950,
            "balance_kcal": -1150.0, "protein_g": 30.0, "carbs_g": 90.0, "fat_g": 20.0,
            "meal_count": 2,
        }
        telegram_bot.dispatch(cfg, _msg(text="/today"), {})
    mock_bal.assert_called_once()
    text = mock_send.call_args.args[1]
    assert "Balance: -1150" in text
    assert "deficit" in text


# ── text → barcode auto-insert ────────────────────────────────────────────


def test_dispatch_routes_numeric_to_barcode_lookup_and_proposes(tmpdb):
    """Numeric barcode → OFF lookup → Confirm/Cancel keyboard. No DB write yet."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    info = {
        "name": "Coca-Cola 330ml",
        "kcal_100g": 42, "protein_100g": 0, "carbs_100g": 10.6, "fat_100g": 0,
        "fiber_100g": None, "sugars_100g": 10.6, "saturated_fat_100g": 0,
        "sodium_mg_100g": None, "food_category": "Sweetened beverages",
        "serving_size_g": 330,
    }
    with patch("src.telegram_bot.food.lookup_barcode", return_value=info) as mock_lookup, \
         patch("src.telegram_bot._send_with_keyboard") as mock_kb:
        telegram_bot.dispatch(cfg, _msg(text="5449000000996"), pending)
    mock_lookup.assert_called_once_with("5449000000996")
    # Nothing inserted yet — proposal is in pending.
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert len(pending) == 1
    proposed = next(iter(pending.values()))["meal"]
    assert proposed["barcode"] == "5449000000996"
    # Keyboard contains both Confirm and Cancel.
    kb = mock_kb.call_args.args[2]
    assert any("confirm:" in b["callback_data"] for b in kb[0])
    assert any("cancel:" in b["callback_data"] for b in kb[0])


def test_dispatch_replies_when_barcode_not_in_off(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot.food.lookup_barcode", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="9999999999"), pending)
    mock_send.assert_called_once()
    assert "not found" in mock_send.call_args.args[1]
    assert pending == {}
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []


# ── text → claude extraction ──────────────────────────────────────────────


def test_dispatch_routes_text_to_llm_and_proposes_confirmation(tmpdb):
    """Free-text → Claude extraction → Confirm/Cancel keyboard. No DB write yet."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    extracted = {"description": "tuna sandwich", "kcal": 480, "protein_g": 28, "carbs_g": 50, "fat_g": 15}
    with patch("src.telegram_bot.llm.extract_meal_from_text", return_value=extracted) as mock_llm, \
         patch("src.telegram_bot._send_with_keyboard") as mock_kb:
        telegram_bot.dispatch(cfg, _msg(text="tuna sandwich on whole wheat"), pending)
    mock_llm.assert_called_once()
    # Nothing in the DB yet; proposal lives in pending.
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert len(pending) == 1
    proposed = next(iter(pending.values()))["meal"]
    assert proposed["description"] == "tuna sandwich"
    assert proposed["source"] == "text"
    kb = mock_kb.call_args.args[2]
    assert any("confirm:" in b["callback_data"] for b in kb[0])
    assert any("cancel:" in b["callback_data"] for b in kb[0])


def test_dispatch_replies_when_llm_returns_none(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot.llm.extract_meal_from_text", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="??"), pending)
    mock_send.assert_called_once()
    assert "couldn't" in mock_send.call_args.args[1].lower()
    assert pending == {}
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []


def test_text_confirm_round_trip_inserts_meal(tmpdb):
    """End-to-end: free-text → propose → tap ✅ → row appears."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    extracted = {"description": "yogurt with granola", "kcal": 320, "protein_g": 12,
                 "carbs_g": 45, "fat_g": 9, "source": "text"}
    with patch("src.telegram_bot.llm.extract_meal_from_text", return_value=extracted), \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(text="yogurt with granola"), pending)
    pid = next(iter(pending))
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard"):
        telegram_bot.dispatch(cfg, _callback(f"confirm:{pid}"), pending)
    rows = db.meals_for_date(tmpdb, date.today().isoformat())
    assert len(rows) == 1
    assert rows[0]["description"] == "yogurt with granola"
    assert rows[0]["source"] == "text"
    assert pending == {}


def test_barcode_cancel_round_trip_drops_proposal(tmpdb):
    """Barcode → proposal → tap ❌ → DB stays empty."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    info = {
        "name": "Snack bar", "kcal_100g": 380, "protein_100g": 6, "carbs_100g": 55,
        "fat_100g": 14, "fiber_100g": 2, "sugars_100g": 30, "saturated_fat_100g": 7,
        "sodium_mg_100g": 120, "food_category": "Sugary snacks", "serving_size_g": 40,
    }
    with patch("src.telegram_bot.food.lookup_barcode", return_value=info), \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(text="1234567890123"), pending)
    pid = next(iter(pending))
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard"):
        telegram_bot.dispatch(cfg, _callback(f"cancel:{pid}"), pending)
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert pending == {}


# ── photo flow ────────────────────────────────────────────────────────────


def test_photo_flow_calls_vision_and_offers_confirmation(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    extracted = {"kind": "meal", "meal": {"description": "plate of pasta", "kcal": 600}}
    with patch("src.telegram_bot._download_telegram_file", return_value=(b"jpegbytes", "image/jpeg")), \
         patch("src.telegram_bot.llm.extract_meal_from_photo", return_value=extracted) as mock_vision, \
         patch("src.telegram_bot._send_with_keyboard") as mock_kb:
        telegram_bot.dispatch(
            cfg,
            _msg(photo=[{"file_id": "fid", "file_size": 100}]),
            pending,
        )
    mock_vision.assert_called_once()
    # No insert yet — pending.
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert len(pending) == 1
    pid = next(iter(pending))
    assert pending[pid]["meal"]["description"] == "plate of pasta"
    assert pending[pid]["meal"]["source"] == "photo"
    # Keyboard contains both Confirm and Cancel.
    kb = mock_kb.call_args.args[2]
    assert any("confirm:" in b["callback_data"] for b in kb[0])
    assert any("cancel:" in b["callback_data"] for b in kb[0])


def test_photo_flow_routes_barcode_through_off(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    info = {
        "name": "Acme bar",
        "kcal_100g": 400, "protein_100g": 5, "carbs_100g": 60, "fat_100g": 14,
        "fiber_100g": 3, "sugars_100g": 35, "saturated_fat_100g": 8,
        "sodium_mg_100g": 80, "food_category": "Sugary snacks",
        "serving_size_g": 50,
    }
    with patch("src.telegram_bot._download_telegram_file", return_value=(b"jpegbytes", "image/jpeg")), \
         patch("src.telegram_bot.llm.extract_meal_from_photo",
               return_value={"kind": "barcode", "barcode": "1234567890123"}), \
         patch("src.telegram_bot.food.lookup_barcode", return_value=info) as mock_lookup, \
         patch("src.telegram_bot._send_with_keyboard") as mock_kb:
        telegram_bot.dispatch(
            cfg,
            _msg(photo=[{"file_id": "fid", "file_size": 100}]),
            pending,
        )
    mock_lookup.assert_called_once_with("1234567890123")
    assert len(pending) == 1
    proposed = list(pending.values())[0]["meal"]
    # Scaled from 50g serving (factor 0.5).
    assert proposed["kcal"] == 200
    assert proposed["food_category"] == "Sugary snacks"
    mock_kb.assert_called_once()


def test_photo_flow_replies_when_vision_returns_none(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot._download_telegram_file", return_value=(b"jpegbytes", "image/jpeg")), \
         patch("src.telegram_bot.llm.extract_meal_from_photo", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(photo=[{"file_id": "fid", "file_size": 100}]), {})
    mock_send.assert_called_once()
    assert "clearer angle" in mock_send.call_args.args[1]


# ── callback flow ─────────────────────────────────────────────────────────


def test_callback_confirm_inserts_pending_meal(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {"meal": {"description": "salmon plate", "kcal": 500, "source": "photo"},
                          "ts": time.time()}}
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("confirm:abc123"), pending)
    assert "abc123" not in pending
    rows = db.meals_for_date(tmpdb, date.today().isoformat())
    assert len(rows) == 1
    assert rows[0]["description"] == "salmon plate"
    mock_ack.assert_called_once()
    mock_edit.assert_called_once()
    assert "Logged" in mock_edit.call_args.args[3]


def test_callback_cancel_drops_pending_without_insert(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {"meal": {"description": "salmon plate", "kcal": 500},
                          "ts": time.time()}}
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("cancel:abc123"), pending)
    assert "abc123" not in pending
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert "Cancelled" in mock_edit.call_args.args[3]


def test_callback_unknown_action_does_not_drop_pending_entry(tmpdb):
    """Bad/unknown callback data must not silently consume a still-valid pending proposal."""
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {"meal": {"description": "hummus", "kcal": 250},
                          "ts": time.time()}}
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit, \
         patch("src.telegram_bot.db.insert_meal") as mock_insert:
        telegram_bot.dispatch(cfg, _callback("foo:abc123"), pending)
    # Action was unknown — entry must still be pending so the user can retry.
    assert pending["abc123"]["meal"]["description"] == "hummus"
    mock_insert.assert_not_called()
    mock_edit.assert_not_called()
    assert mock_ack.call_args.args[2] == "Unknown action"


def test_callback_expired_pending_replies_gracefully(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("confirm:gone"), {})  # empty pending
    assert mock_ack.call_args.args[2] == "Expired"
    assert "expired" in mock_edit.call_args.args[3].lower()
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []


# ── confirm we never persist photo bytes ──────────────────────────────────


def test_photo_flow_does_not_leak_image_bytes_to_db(tmpdb):
    """Sanity: after a photo round-trip + confirm, the meals row has no base64 data."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    extracted = {"kind": "meal", "meal": {"description": "noodles", "kcal": 400}}
    with patch("src.telegram_bot._download_telegram_file", return_value=(b"\x00" * 50000, "image/jpeg")), \
         patch("src.telegram_bot.llm.extract_meal_from_photo", return_value=extracted), \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(photo=[{"file_id": "fid", "file_size": 100}]), pending)
    pid = next(iter(pending))
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard"):
        telegram_bot.dispatch(cfg, _callback(f"confirm:{pid}"), pending)
    rows = db.meals_for_date(tmpdb, date.today().isoformat())
    assert len(rows) == 1
    assert rows[0]["raw_json"] in (None, "")  # never populated by the bot
    # No table named photo_*: every row in sqlite_master should be a known table.
    import sqlite3
    with sqlite3.connect(tmpdb) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert not any(n.startswith("photo") for n in names)
