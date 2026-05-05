"""Tests for src/telegram_bot.py — chat-first architecture.

Every Telegram, Anthropic, and Open Food Facts call is mocked. The tests
poke `dispatch()` directly with synthetic Telegram update payloads rather
than spinning up the long-poll loop.

After the chat-first refactor:
- Plain text and photos default to `llm.chat()` with the full 12-tool surface.
- Write tools (log_meal/edit_meal/delete_meal) park proposals in `pending`
  via `llm._stash_pending`; the bot diffs `pending` before/after each chat
  call and surfaces a Confirm/Cancel inline keyboard for each new proposal.
- `/help`, `/start`, `/today`, `/balance`, and numeric `^\\d{8,14}$` are
  fast paths that bypass Claude.
"""
from __future__ import annotations

import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

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


def _msg(text: str | None = None, photo: list[dict] | None = None,
         caption: str | None = None, chat_id: int = 42) -> dict:
    body: dict = {"update_id": 1, "message": {"chat": {"id": chat_id}, "message_id": 99}}
    if text is not None:
        body["message"]["text"] = text
    if photo is not None:
        body["message"]["photo"] = photo
    if caption is not None:
        body["message"]["caption"] = caption
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
    """Non-whitelisted chat IDs must not reach any handler — and especially must not call Claude."""
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.alerts.send_telegram") as mock_send, \
         patch("src.telegram_bot.llm.chat") as mock_chat:
        telegram_bot.dispatch(cfg, _msg(text="hello", chat_id=999), {})
    mock_send.assert_not_called()
    mock_chat.assert_not_called()


# ── fast-path commands ────────────────────────────────────────────────────


def test_help_command_sends_help_text(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.alerts.send_telegram") as mock_send, \
         patch("src.telegram_bot.llm.chat") as mock_chat:
        telegram_bot.dispatch(cfg, _msg(text="/help"), {})
    mock_send.assert_called_once()
    text = mock_send.call_args.args[1]
    assert "fitness & nutrition" in text
    mock_chat.assert_not_called()


def test_today_command_uses_fast_path_not_chat(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.alerts.send_telegram") as mock_send, \
         patch("src.telegram_bot.db.calorie_balance_for_date") as mock_bal, \
         patch("src.telegram_bot.llm.chat") as mock_chat:
        mock_bal.return_value = {
            "date": "2026-05-05", "eaten_kcal": 800.0, "burned_kcal": 0,
            "bmr_kcal": 1700, "steps_burned_kcal": 250, "total_burned_kcal": 1950,
            "balance_kcal": -1150.0, "protein_g": 30.0, "carbs_g": 90.0, "fat_g": 20.0,
            "meal_count": 2,
        }
        telegram_bot.dispatch(cfg, _msg(text="/today"), {})
    mock_bal.assert_called_once()
    mock_chat.assert_not_called()
    text = mock_send.call_args.args[1]
    assert "Balance: -1150" in text
    assert "deficit" in text


# ── numeric barcode fast path ─────────────────────────────────────────────


def test_numeric_barcode_proposes_via_off_without_calling_chat(tmpdb):
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
         patch("src.telegram_bot._send_with_keyboard") as mock_kb, \
         patch("src.telegram_bot.llm.chat") as mock_chat:
        telegram_bot.dispatch(cfg, _msg(text="5449000000996"), pending)
    mock_lookup.assert_called_once_with("5449000000996")
    mock_chat.assert_not_called()
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert len(pending) == 1
    pid = next(iter(pending))
    assert pending[pid]["action"] == "insert"
    assert pending[pid]["meal"]["barcode"] == "5449000000996"
    kb = mock_kb.call_args.args[2]
    assert any("confirm:" in b["callback_data"] for b in kb[0])


def test_numeric_barcode_replies_when_not_in_off(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot.food.lookup_barcode", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="9999999999"), pending)
    mock_send.assert_called_once()
    assert "not found" in mock_send.call_args.args[1]
    assert pending == {}


# ── chat default for plain text ───────────────────────────────────────────


def test_plain_text_routes_to_chat(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot.llm.chat", return_value="Logged: yogurt with granola — 320 kcal.") as mock_chat, \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="yogurt with granola"), pending)
    # chat is called with cfg, the user text, and the same pending dict.
    args, kwargs = mock_chat.call_args
    assert args[0] is cfg
    assert args[1] == "yogurt with granola"
    assert args[2] is pending
    assert kwargs.get("image_bytes") is None
    mock_send.assert_called_once()
    assert "Logged: yogurt" in mock_send.call_args.args[1]


def test_plain_text_surfaces_keyboard_for_each_new_pending_entry(tmpdb):
    """When chat() parks a write proposal in `pending`, the bot must surface a Confirm/Cancel keyboard."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    proposal = {"action": "insert", "meal": {"description": "oats", "kcal": 350, "source": "ai"}, "ts": time.time()}

    def fake_chat(cfg_, text, p, **kw):
        p["abc123"] = proposal
        return "Logged your oats."

    with patch("src.telegram_bot.llm.chat", side_effect=fake_chat), \
         patch("src.telegram_bot._send_plain") as mock_send, \
         patch("src.telegram_bot._send_with_keyboard") as mock_kb:
        telegram_bot.dispatch(cfg, _msg(text="oats 350 kcal"), pending)

    mock_send.assert_called_once()  # the "Logged your oats." reply
    mock_kb.assert_called_once()    # the Confirm/Cancel keyboard for the new pending entry
    body = mock_kb.call_args.args[1]
    assert "oats" in body
    kb = mock_kb.call_args.args[2]
    assert any(b["callback_data"] == "confirm:abc123" for b in kb[0])
    assert any(b["callback_data"] == "cancel:abc123" for b in kb[0])


def test_plain_text_replies_with_failure_when_chat_returns_none(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot.llm.chat", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="hello"), pending)
    mock_send.assert_called_once()
    assert "couldn't reach Claude" in mock_send.call_args.args[1]


def test_plain_text_truncates_long_replies(tmpdb):
    cfg = _make_cfg(tmpdb)
    long_reply = "x" * 5000
    with patch("src.telegram_bot.llm.chat", return_value=long_reply), \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="hello"), {})
    sent = mock_send.call_args.args[1]
    assert len(sent) <= 4100  # 4000 + the "[…truncated]" suffix
    assert sent.endswith("[…truncated]")


# ── photo flow → chat with image ──────────────────────────────────────────


def test_photo_dispatched_to_chat_with_image_bytes(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot._download_telegram_file", return_value=(b"jpegbytes", "image/jpeg")), \
         patch("src.telegram_bot.llm.chat", return_value="I see eggs and toast.") as mock_chat, \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(
            cfg,
            _msg(photo=[{"file_id": "fid", "file_size": 100}], caption="lunch"),
            pending,
        )
    # Photo bytes propagated to chat as a kwarg; pending dict shared.
    args, kwargs = mock_chat.call_args
    assert args[0] is cfg
    assert args[1] == "lunch"
    assert args[2] is pending
    assert kwargs["image_bytes"] == b"jpegbytes"
    assert kwargs["mime_type"] == "image/jpeg"


def test_photo_replies_when_download_fails(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot._download_telegram_file", return_value=(None, "")), \
         patch("src.telegram_bot.llm.chat") as mock_chat, \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(photo=[{"file_id": "fid", "file_size": 100}]), {})
    mock_chat.assert_not_called()
    mock_send.assert_called_once()
    assert "Couldn't download" in mock_send.call_args.args[1]


# ── callback flow: confirm + cancel for every action type ────────────────


def test_callback_confirm_insert_writes_meal(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {
        "action": "insert",
        "meal": {"description": "salmon plate", "kcal": 500, "source": "ai"},
        "ts": time.time(),
    }}
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("confirm:abc123"), pending)
    assert "abc123" not in pending
    rows = db.meals_for_date(tmpdb, date.today().isoformat())
    assert len(rows) == 1
    assert rows[0]["description"] == "salmon plate"
    assert mock_ack.call_args.args[2] == "Logged"
    assert "Logged" in mock_edit.call_args.args[3]


def test_callback_confirm_edit_applies_partial_update(tmpdb):
    cfg = _make_cfg(tmpdb)
    mid = db.insert_meal(tmpdb, {"description": "porridge", "source": "manual", "kcal": 350})
    pending = {"e1": {
        "action": "edit",
        "meal_id": mid,
        "fields": {"kcal": 320, "description": "porridge (corrected)"},
        "before": db.get_meal_by_id(tmpdb, mid),
        "ts": time.time(),
    }}
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("confirm:e1"), pending)
    after = db.get_meal_by_id(tmpdb, mid)
    assert after["kcal"] == 320
    assert after["description"] == "porridge (corrected)"
    assert mock_ack.call_args.args[2] == "Updated"
    assert "Updated meal" in mock_edit.call_args.args[3]


def test_callback_confirm_delete_removes_row(tmpdb):
    cfg = _make_cfg(tmpdb)
    mid = db.insert_meal(tmpdb, {"description": "snack", "source": "manual", "kcal": 200})
    pending = {"d1": {
        "action": "delete",
        "meal_id": mid,
        "before": db.get_meal_by_id(tmpdb, mid),
        "ts": time.time(),
    }}
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("confirm:d1"), pending)
    assert db.get_meal_by_id(tmpdb, mid) is None
    assert mock_ack.call_args.args[2] == "Deleted"
    assert "Deleted meal" in mock_edit.call_args.args[3]


def test_callback_cancel_drops_pending_without_writing(tmpdb):
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {"action": "insert", "meal": {"description": "x", "kcal": 100}, "ts": time.time()}}
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("cancel:abc123"), pending)
    assert pending == {}
    assert db.meals_for_date(tmpdb, date.today().isoformat()) == []
    assert "Cancelled" in mock_edit.call_args.args[3]


def test_callback_unknown_action_does_not_drop_pending_entry(tmpdb):
    """Bad callback data must not consume a still-valid pending proposal."""
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {"action": "insert", "meal": {"description": "hummus", "kcal": 250},
                          "ts": time.time()}}
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit, \
         patch("src.telegram_bot.db.insert_meal") as mock_insert:
        telegram_bot.dispatch(cfg, _callback("foo:abc123"), pending)
    assert pending["abc123"]["meal"]["description"] == "hummus"
    mock_insert.assert_not_called()
    mock_edit.assert_not_called()
    assert mock_ack.call_args.args[2] == "Unknown action"


def test_callback_expired_pending_replies_gracefully(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        telegram_bot.dispatch(cfg, _callback("confirm:gone"), {})
    assert mock_ack.call_args.args[2] == "Expired"
    assert "expired" in mock_edit.call_args.args[3].lower()


# ── end-to-end chains ─────────────────────────────────────────────────────


def test_text_to_chat_to_confirm_round_trip_inserts_meal(tmpdb):
    """Plain text → chat parks a log_meal proposal → tap ✅ → meal lands in DB."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}

    def fake_chat(cfg_, text, p, **kw):
        p["abc123"] = {
            "action": "insert",
            "meal": {"description": "tuna sandwich", "kcal": 480, "source": "ai"},
            "ts": time.time(),
        }
        return "Logged: tuna sandwich — 480 kcal."

    with patch("src.telegram_bot.llm.chat", side_effect=fake_chat), \
         patch("src.telegram_bot._send_plain"), \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(text="tuna sandwich on whole wheat"), pending)
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard"):
        telegram_bot.dispatch(cfg, _callback("confirm:abc123"), pending)
    rows = db.meals_for_date(tmpdb, date.today().isoformat())
    assert len(rows) == 1
    assert rows[0]["description"] == "tuna sandwich"


def test_photo_flow_does_not_leak_image_bytes_to_db(tmpdb):
    """Round-trip a photo through chat + confirm — assert no raw bytes anywhere on disk."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}

    def fake_chat(cfg_, text, p, **kw):
        # The bot must pass image_bytes through; simulate a log_meal proposal.
        assert kw.get("image_bytes") == b"\x00" * 50000
        p["pid1"] = {
            "action": "insert",
            "meal": {"description": "noodles", "kcal": 400, "source": "ai"},
            "ts": time.time(),
        }
        return "Logged: noodles."

    with patch("src.telegram_bot._download_telegram_file", return_value=(b"\x00" * 50000, "image/jpeg")), \
         patch("src.telegram_bot.llm.chat", side_effect=fake_chat), \
         patch("src.telegram_bot._send_plain"), \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(photo=[{"file_id": "fid", "file_size": 100}]), pending)
    with patch("src.telegram_bot._answer_callback"), \
         patch("src.telegram_bot._edit_message_remove_keyboard"):
        telegram_bot.dispatch(cfg, _callback("confirm:pid1"), pending)
    rows = db.meals_for_date(tmpdb, date.today().isoformat())
    assert len(rows) == 1
    assert rows[0]["raw_json"] in (None, "")  # bot never populates raw_json
    import sqlite3
    with sqlite3.connect(tmpdb) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert not any(n.startswith("photo") for n in names)
