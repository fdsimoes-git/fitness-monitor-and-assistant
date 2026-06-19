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
import requests

from src import db, telegram_bot
from src.config import Config


@pytest.fixture(autouse=True)
def _no_real_telegram_http(monkeypatch):
    """Default-stub the bot's outbound HTTP helpers so no test ever talks to
    api.telegram.org. Individual tests can still re-patch any of these to
    assert behavior; this fixture only protects tests that don't explicitly
    care about a given helper.

    NOTE: `_edit_message_remove_keyboard` is intentionally NOT stubbed here —
    every test that exercises the callback path patches it explicitly
    (asserting the keyboard-clearing behavior is part of the contract), and
    leaving it un-stubbed lets `test_edit_message_remove_keyboard_clears_inline_keyboard`
    exercise the real implementation against a mocked requests.post. As
    belt-and-braces, we also stub `requests.post` so any code path that
    forgets to patch a helper still can't reach the network.
    """
    monkeypatch.setattr("src.telegram_bot._send_typing", lambda cfg: None)
    monkeypatch.setattr("src.telegram_bot._send_status", lambda cfg, text: 1)
    monkeypatch.setattr("src.telegram_bot._edit_status", lambda cfg, msg_id, text: None)
    monkeypatch.setattr("src.telegram_bot._delete_message", lambda cfg, msg_id: None)
    monkeypatch.setattr("src.telegram_bot._answer_callback", lambda cfg, cid, text: None)
    # Catch-all: even if a helper isn't stubbed, no test gets to api.telegram.org.
    monkeypatch.setattr(
        "src.telegram_bot.requests.post",
        lambda *a, **kw: type("R", (), {"raise_for_status": lambda self: None,
                                         "json": lambda self: {"ok": True, "result": {}},
                                         "content": b""})(),
    )


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


def test_today_command_routes_through_db_local_today(tmpdb):
    """The /today fast path must use db.local_today (the patchable seam)
    rather than calling date.today() directly."""
    cfg = _make_cfg(tmpdb)
    from datetime import date as _date
    fake_today = _date(2026, 5, 5)
    with patch("src.telegram_bot.db.local_today", return_value=fake_today) as mock_local, \
         patch("src.telegram_bot.db.calorie_balance_for_date") as mock_bal, \
         patch("src.telegram_bot.alerts.send_telegram"):
        mock_bal.return_value = {
            "date": "2026-05-05", "eaten_kcal": 0, "burned_kcal": 0,
            "bmr_kcal": 0, "steps_burned_kcal": 0, "total_burned_kcal": 0,
            "balance_kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0,
            "meal_count": 0,
        }
        telegram_bot.dispatch(cfg, _msg(text="/today"), {})
    mock_local.assert_called_once()
    # And the date string handed to calorie_balance_for_date came from local_today.
    assert mock_bal.call_args.args[1] == "2026-05-05"


def test_edit_message_remove_keyboard_clears_inline_keyboard(tmpdb):
    """Telegram keeps the existing reply_markup unless an explicit
    replacement is sent. The helper must pass {"inline_keyboard": []} so
    Confirm/Cancel buttons stop being clickable after the user already
    chose one — otherwise stale taps trigger 'Expired' callbacks."""
    cfg = _make_cfg(tmpdb)
    captured = {}

    def capture(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return type("R", (), {"raise_for_status": lambda self: None})()

    with patch("src.telegram_bot.requests.post", side_effect=capture):
        telegram_bot._edit_message_remove_keyboard(cfg, 42, 99, "Done.")
    assert captured["url"].endswith("/editMessageText")
    assert captured["json"]["text"] == "Done."
    assert captured["json"]["reply_markup"] == {"inline_keyboard": []}


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
         patch("src.telegram_bot._edit_status") as mock_edit:
        telegram_bot.dispatch(cfg, _msg(text="yogurt with granola"), pending)
    args, kwargs = mock_chat.call_args
    assert args[0] is cfg
    assert args[1] == "yogurt with granola"
    assert args[2] is pending
    assert kwargs.get("image_bytes") is None
    # Final answer is edited into the thinking message rather than sent fresh.
    mock_edit.assert_called_once()
    assert "Logged: yogurt" in mock_edit.call_args.args[2]


def test_plain_text_surfaces_keyboard_for_each_new_pending_entry(tmpdb):
    """When chat() parks a write proposal in `pending`, the bot must surface a Confirm/Cancel keyboard."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    proposal = {"action": "insert", "meal": {"description": "oats", "kcal": 350, "source": "ai"}, "ts": time.time()}

    def fake_chat(cfg_, text, p, **kw):
        p["abc123"] = proposal
        return "Logged your oats."

    with patch("src.telegram_bot.llm.chat", side_effect=fake_chat), \
         patch("src.telegram_bot._edit_status") as mock_edit, \
         patch("src.telegram_bot._send_with_keyboard") as mock_kb:
        telegram_bot.dispatch(cfg, _msg(text="oats 350 kcal"), pending)

    # Final answer goes via edit (into the thinking message)…
    mock_edit.assert_called_once()
    assert "Logged your oats" in mock_edit.call_args.args[2]
    # …and a separate keyboard for the proposal.
    mock_kb.assert_called_once()
    body = mock_kb.call_args.args[1]
    assert "oats" in body
    kb = mock_kb.call_args.args[2]
    assert any(b["callback_data"] == "confirm:abc123" for b in kb[0])
    assert any(b["callback_data"] == "cancel:abc123" for b in kb[0])


def test_plain_text_replies_with_failure_when_chat_returns_none(tmpdb):
    """No proposals AND no text → the thinking message is edited into a clear failure."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    with patch("src.telegram_bot.llm.chat", return_value=None), \
         patch("src.telegram_bot._edit_status") as mock_edit:
        telegram_bot.dispatch(cfg, _msg(text="hello"), pending)
    mock_edit.assert_called_once()
    assert "couldn't reach Claude" in mock_edit.call_args.args[2]


def test_plain_text_truncates_long_replies(tmpdb):
    cfg = _make_cfg(tmpdb)
    long_reply = "x" * 5000
    with patch("src.telegram_bot.llm.chat", return_value=long_reply), \
         patch("src.telegram_bot._edit_status") as mock_edit:
        telegram_bot.dispatch(cfg, _msg(text="hello"), {})
    sent = mock_edit.call_args.args[2]
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


def test_callback_confirm_recovers_when_apply_pending_raises(tmpdb):
    """If db.insert_meal blows up under a confirmed proposal, the bot must still
    answer the callback (no hung spinner) and edit the keyboard into a clear
    failure message — no exception escapes _handle_callback."""
    cfg = _make_cfg(tmpdb)
    pending = {"abc123": {
        "action": "insert",
        "meal": {"description": "salmon", "kcal": 500, "source": "ai"},
        "ts": time.time(),
    }}
    with patch("src.telegram_bot.db.insert_meal", side_effect=RuntimeError("disk full")), \
         patch("src.telegram_bot._answer_callback") as mock_ack, \
         patch("src.telegram_bot._edit_message_remove_keyboard") as mock_edit:
        # Should NOT raise.
        telegram_bot.dispatch(cfg, _callback("confirm:abc123"), pending)
    mock_ack.assert_called_once()
    assert mock_ack.call_args.args[2] == "Failed"
    mock_edit.assert_called_once()
    assert "disk full" in mock_edit.call_args.args[3]


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


# ── pending TTL sweep ─────────────────────────────────────────────────────


# ── live progress feedback ────────────────────────────────────────────────


def test_run_chat_posts_thinking_status_and_passes_progress_cb(tmpdb):
    """Bot creates a status message, threads its msg_id through a progress_cb
    that calls _edit_status, and edits the final answer into the same message."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []
    captured = {}

    def fake_chat(cfg_, text, p, **kw):
        # Simulate the loop: progress_cb fires once per tool call.
        cb = kw["progress_cb"]
        cb("get_balance", {})
        cb("lookup_barcode", {"barcode": "5449000000996"})
        return "All set."

    with patch("src.telegram_bot.llm.chat", side_effect=fake_chat) as mock_chat, \
         patch("src.telegram_bot._send_status", return_value=42) as mock_status, \
         patch("src.telegram_bot._edit_status") as mock_edit:
        telegram_bot.dispatch(cfg, _msg(text="how am I doing?"), pending, history)

    # Status message was sent at the start with the thinking placeholder.
    mock_status.assert_called_once()
    assert mock_status.call_args.args[1] == telegram_bot.THINKING_TEXT

    # progress_cb was supplied to chat()
    assert callable(mock_chat.call_args.kwargs["progress_cb"])

    # Three edit_status calls: 2 tool updates + 1 final answer
    assert mock_edit.call_count == 3
    update_texts = [c.args[2] for c in mock_edit.call_args_list]
    assert any("balance" in t for t in update_texts)
    assert any("5449000000996" in t for t in update_texts)
    # Final edit replaces the status with the answer text.
    assert update_texts[-1] == "All set."


def test_run_chat_deletes_status_when_no_text_and_proposals_present(tmpdb):
    """Model parked a write proposal but didn't return text → status message
    is deleted (the keyboard alone is the visible feedback)."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []

    def fake_chat(cfg_, text, p, **kw):
        p["pid1"] = {
            "action": "insert",
            "meal": {"description": "oats", "kcal": 350},
            "ts": time.time(),
        }
        return None

    with patch("src.telegram_bot.llm.chat", side_effect=fake_chat), \
         patch("src.telegram_bot._send_status", return_value=99), \
         patch("src.telegram_bot._delete_message") as mock_del, \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(text="oats"), pending, history)

    mock_del.assert_called_once_with(cfg, 99)


def test_format_tool_status_includes_specific_arguments():
    assert "5449000000996" in telegram_bot._format_tool_status(
        "lookup_barcode", {"barcode": "5449000000996"}
    )
    assert "#42" in telegram_bot._format_tool_status("edit_meal", {"meal_id": 42})
    assert "#42" in telegram_bot._format_tool_status("delete_meal", {"meal_id": 42})
    # Generic tools fall back to the static label
    assert telegram_bot._format_tool_status("get_balance", {}).startswith("💰")


def test_run_chat_falls_back_to_send_plain_when_status_message_fails(tmpdb):
    """If _send_status returns None (Telegram down), chat still runs and the
    answer goes through the regular _send_plain path — no UX regression."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []
    with patch("src.telegram_bot.llm.chat", return_value="Hello back.") as mock_chat, \
         patch("src.telegram_bot._send_status", return_value=None), \
         patch("src.telegram_bot._send_plain") as mock_send, \
         patch("src.telegram_bot._edit_status") as mock_edit:
        telegram_bot.dispatch(cfg, _msg(text="hello"), pending, history)
    # progress_cb is None when status couldn't be created
    assert mock_chat.call_args.kwargs["progress_cb"] is None
    mock_edit.assert_not_called()
    mock_send.assert_called_once()
    assert mock_send.call_args.args[1] == "Hello back."


# ── /ask & /chat legacy prefix stripping ─────────────────────────────────


def test_dispatch_strips_ask_prefix_before_forwarding_to_chat(tmpdb):
    """Users who still type "/ask <question>" out of habit should have the
    prefix stripped — the model shouldn't see "/ask" in its context."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []
    with patch("src.telegram_bot.llm.chat", return_value="ok") as mock_chat:
        telegram_bot.dispatch(cfg, _msg(text="/ask How's my protein this week?"), pending, history)
    args, _ = mock_chat.call_args
    assert args[1] == "How's my protein this week?"  # /ask stripped


def test_dispatch_strips_chat_alias_too(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.llm.chat", return_value="ok") as mock_chat:
        telegram_bot.dispatch(cfg, _msg(text="/chat   what should I eat?"), {}, [])
    assert mock_chat.call_args.args[1] == "what should I eat?"


def test_dispatch_ask_alone_sends_usage_hint(tmpdb):
    cfg = _make_cfg(tmpdb)
    with patch("src.telegram_bot.llm.chat") as mock_chat, \
         patch("src.telegram_bot._send_plain") as mock_send:
        telegram_bot.dispatch(cfg, _msg(text="/ask"), {}, [])
    mock_chat.assert_not_called()
    mock_send.assert_called_once()
    assert "no /ask needed" in mock_send.call_args.args[1]


# ── _format_meal_summary defensive numeric coercion ───────────────────────


def test_format_meal_summary_handles_stringly_typed_numbers():
    """LLM tool input may stringify numbers; the formatter must not crash."""
    s = telegram_bot._format_meal_summary({
        "description": "oats",
        "kcal": "350",          # string
        "protein_g": "12.5",    # string with decimal
        "carbs_g": 60,          # already numeric
        "fat_g": "abc",         # gibberish — should fall back, not raise
    })
    assert "oats" in s
    assert "350" in s
    assert "12g" in s or "13g" in s  # rounded
    assert "60g" in s
    # `abc` falls back to the value's repr rather than crashing.
    assert "abc" in s


# ── chat history ──────────────────────────────────────────────────────────


def test_chat_history_grows_on_each_chat_turn(tmpdb):
    """Plain text → chat appends a (user, assistant) pair to history."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []
    with patch("src.telegram_bot.llm.chat", return_value="Got it.") as mock_chat, \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(cfg, _msg(text="hello"), pending, history)
    # The chat call received the (initially empty) history.
    assert mock_chat.call_args.kwargs["history"] is history
    # After the call, history grew by exactly one user/assistant pair.
    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Got it."},
    ]


def test_chat_history_caps_at_10_pairs(tmpdb):
    """Adding more than HISTORY_MAX_PAIRS pairs trims the oldest."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    # Pre-fill with 10 pairs (= 20 messages).
    history: list[dict] = []
    for i in range(10):
        history.append({"role": "user", "content": f"old user {i}"})
        history.append({"role": "assistant", "content": f"old assistant {i}"})

    with patch("src.telegram_bot.llm.chat", return_value="new reply"), \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(cfg, _msg(text="new user msg"), pending, history)

    assert len(history) == telegram_bot.HISTORY_MAX_PAIRS * 2
    # Oldest pair was evicted; newest is at the end.
    assert history[0] == {"role": "user", "content": "old user 1"}
    assert history[-2] == {"role": "user", "content": "new user msg"}
    assert history[-1] == {"role": "assistant", "content": "new reply"}


def test_chat_history_stores_photo_as_placeholder_not_bytes(tmpdb):
    """Photo turns must NOT push image bytes into history — only a text placeholder."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []
    with patch("src.telegram_bot._download_telegram_file", return_value=(b"\x89PNGfake" * 1000, "image/png")), \
         patch("src.telegram_bot.llm.chat", return_value="Looks like pasta.") as mock_chat, \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(
            cfg,
            _msg(photo=[{"file_id": "fid", "file_size": 100}], caption="lunch"),
            pending,
            history,
        )
    # llm.chat saw the bytes — that's expected.
    assert mock_chat.call_args.kwargs["image_bytes"] is not None
    # But history must hold ONLY text — no bytes anywhere.
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "[photo] lunch"
    assert isinstance(history[0]["content"], str)
    assert history[1]["content"] == "Looks like pasta."


def test_chat_history_skips_failed_turns_entirely(tmpdb):
    """When chat returns no text AND no new proposals, history must not get a
    misleading 'proposed action' placeholder — the failed turn is dropped."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []
    with patch("src.telegram_bot.llm.chat", return_value=None), \
         patch("src.telegram_bot._send_status", return_value=None), \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(cfg, _msg(text="hello"), pending, history)
    assert history == []


def test_chat_history_records_placeholder_when_no_text_reply(tmpdb):
    """Model went straight to a write tool with no text — history still gets a paired entry."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []

    def fake_chat(cfg_, text, p, **kw):
        p["abc"] = {
            "action": "insert",
            "meal": {"description": "oats", "kcal": 350},
            "ts": time.time(),
        }
        return None  # silent: model only made a tool call

    with patch("src.telegram_bot.llm.chat", side_effect=fake_chat), \
         patch("src.telegram_bot._send_plain"), \
         patch("src.telegram_bot._send_with_keyboard"):
        telegram_bot.dispatch(cfg, _msg(text="oats 350"), pending, history)

    assert history == [
        {"role": "user", "content": "oats 350"},
        {"role": "assistant", "content": "(proposed action — awaiting user confirmation)"},
    ]


def test_help_today_balance_and_barcode_do_not_pollute_history(tmpdb):
    """Fast-path commands shouldn't leak into chat history."""
    cfg = _make_cfg(tmpdb)
    pending: dict[str, dict] = {}
    history: list[dict] = []

    with patch("src.telegram_bot.alerts.send_telegram"), \
         patch("src.telegram_bot.db.calorie_balance_for_date") as mock_bal:
        mock_bal.return_value = {
            "date": "2026-05-05", "eaten_kcal": 0, "burned_kcal": 0,
            "bmr_kcal": 0, "steps_burned_kcal": 0, "total_burned_kcal": 0,
            "balance_kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0,
            "meal_count": 0,
        }
        telegram_bot.dispatch(cfg, _msg(text="/help"), pending, history)
        telegram_bot.dispatch(cfg, _msg(text="/today"), pending, history)
        telegram_bot.dispatch(cfg, _msg(text="/balance"), pending, history)
    with patch("src.telegram_bot.food.lookup_barcode", return_value=None), \
         patch("src.telegram_bot._send_plain"):
        telegram_bot.dispatch(cfg, _msg(text="9999999999"), pending, history)

    assert history == []


def test_dispatch_sweeps_stale_pending_entries(tmpdb):
    """Every inbound update triggers a TTL sweep — even ones that originated
    from the chat path (which used to skip _sweep_pending entirely)."""
    cfg = _make_cfg(tmpdb)
    # One stale entry (older than PENDING_TTL_SECONDS) and one fresh.
    pending = {
        "stale": {"action": "insert", "meal": {"description": "x", "kcal": 1},
                  "ts": time.time() - telegram_bot.PENDING_TTL_SECONDS - 60},
        "fresh": {"action": "insert", "meal": {"description": "y", "kcal": 2},
                  "ts": time.time()},
    }
    # /help is the cheapest update — still goes through dispatch().
    with patch("src.telegram_bot.alerts.send_telegram"):
        telegram_bot.dispatch(cfg, _msg(text="/help"), pending)
    assert "stale" not in pending
    assert "fresh" in pending


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


# ── _retry_transient tests ────────────────────────────────────────────────


def test_retry_transient_retries_on_timeout(monkeypatch):
    """_retry_transient retries on Timeout and sleeps between attempts."""
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=[
        requests.exceptions.Timeout("timeout"),
        "ok",
    ])
    sleep_mock = MagicMock()
    # _retry_transient uses src.telegram_bot.time.sleep — that's the only
    # binding worth patching.
    import src.telegram_bot as _tb
    monkeypatch.setattr(_tb.time, "sleep", sleep_mock)

    result = _retry_transient(fn, retries=2, backoff=0.5)
    assert result == "ok"
    assert fn.call_count == 2
    sleep_mock.assert_called_once_with(0.5)


def test_retry_transient_retries_on_connection_error(monkeypatch):
    """_retry_transient retries on ConnectionError."""
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=[
        requests.exceptions.ConnectionError("conn"),
        requests.exceptions.ConnectionError("conn"),
        "ok",
    ])
    sleep_mock = MagicMock()
    import src.telegram_bot as _tb
    monkeypatch.setattr(_tb.time, "sleep", sleep_mock)

    result = _retry_transient(fn, retries=2, backoff=1.0)
    assert result == "ok"
    assert fn.call_count == 3
    assert sleep_mock.call_count == 2


def test_retry_transient_raises_after_exhaustion(monkeypatch):
    """_retry_transient raises after all retries are exhausted."""
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=requests.exceptions.Timeout("timeout"))
    import src.telegram_bot as _tb
    monkeypatch.setattr(_tb.time, "sleep", MagicMock())

    with pytest.raises(requests.exceptions.Timeout):
        _retry_transient(fn, retries=1)
    assert fn.call_count == 2


def _http_error(status: int) -> requests.exceptions.HTTPError:
    """Build an HTTPError with a response carrying the given status code."""
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"{status} error", response=resp)


def test_retry_transient_retries_on_5xx(monkeypatch):
    """_retry_transient retries when the HTTPError carries a 5xx status."""
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=[_http_error(500), "ok"])
    import src.telegram_bot as _tb
    monkeypatch.setattr(_tb.time, "sleep", MagicMock())

    result = _retry_transient(fn, retries=1)
    assert result == "ok"
    assert fn.call_count == 2


def test_retry_transient_retries_on_429(monkeypatch):
    """Rate-limit responses are transient — retry them."""
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=[_http_error(429), "ok"])
    import src.telegram_bot as _tb
    monkeypatch.setattr(_tb.time, "sleep", MagicMock())

    result = _retry_transient(fn, retries=1)
    assert result == "ok"
    assert fn.call_count == 2


def test_retry_transient_does_not_retry_4xx(monkeypatch):
    """4xx client errors (except 429) are permanent — re-raise immediately."""
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=_http_error(400))
    import src.telegram_bot as _tb
    sleep_mock = MagicMock()
    monkeypatch.setattr(_tb.time, "sleep", sleep_mock)

    with pytest.raises(requests.exceptions.HTTPError):
        _retry_transient(fn, retries=2)
    # No retries attempted, no sleep.
    assert fn.call_count == 1
    sleep_mock.assert_not_called()


def test_retry_transient_does_not_retry_404(monkeypatch):
    from unittest.mock import MagicMock
    from src.telegram_bot import _retry_transient

    fn = MagicMock(side_effect=_http_error(404))
    import src.telegram_bot as _tb
    monkeypatch.setattr(_tb.time, "sleep", MagicMock())

    with pytest.raises(requests.exceptions.HTTPError):
        _retry_transient(fn, retries=2)
    assert fn.call_count == 1


def test_check_telegram_ok_raises_on_ok_false():
    """Telegram returning HTTP 200 with ok:false must raise so the failure
    surfaces — raise_for_status alone doesn't catch this case."""
    from src.telegram_bot import _check_telegram_ok, _TelegramAPIError

    r = requests.Response()
    r.status_code = 200
    r._content = b'{"ok": false, "error_code": 400, "description": "message to edit not found"}'
    with pytest.raises(_TelegramAPIError, match="message to edit not found"):
        _check_telegram_ok(r)


def test_check_telegram_ok_passes_on_ok_true():
    from src.telegram_bot import _check_telegram_ok

    r = requests.Response()
    r.status_code = 200
    r._content = b'{"ok": true, "result": {}}'
    # No exception.
    _check_telegram_ok(r)


def test_check_telegram_ok_raises_when_ok_field_missing():
    """A 200 JSON object without an `ok` field is just as broken as ok:false —
    treat any body that isn't explicitly ok:true as an application error."""
    from src.telegram_bot import _check_telegram_ok, _TelegramAPIError

    r = requests.Response()
    r.status_code = 200
    r._content = b'{"result": {}}'
    with pytest.raises(_TelegramAPIError, match="ok!=true"):
        _check_telegram_ok(r)


def test_check_telegram_ok_raises_on_non_dict_json():
    """Telegram's contract is a JSON object; a list/scalar 200 body indicates
    something is very wrong upstream and must not be treated as success."""
    from src.telegram_bot import _check_telegram_ok, _TelegramAPIError

    r = requests.Response()
    r.status_code = 200
    r._content = b'[1, 2, 3]'
    with pytest.raises(_TelegramAPIError, match="non-object JSON body"):
        _check_telegram_ok(r)


# ── _redact token sanitization ────────────────────────────────────────────


def test_redact_strips_bot_token_from_telegram_url():
    """A full Telegram API URL containing the bot token must be rewritten so
    the token is replaced with '<redacted>' — otherwise HTTPError stringifies
    leak credentials into log files."""
    from src.telegram_bot import _redact

    msg = (
        "500 Server Error: Internal Server Error for url: "
        "https://api.telegram.org/bot123456:ABC-DEF_secret/sendMessage"
    )
    out = _redact(msg)
    assert "123456:ABC-DEF_secret" not in out
    assert "https://api.telegram.org/bot<redacted>/sendMessage" in out


def test_redact_strips_token_from_file_download_url():
    """The /file/bot<TOKEN>/<path> variant (used by getFile downloads) must
    also be redacted — the regex covers both URL shapes."""
    from src.telegram_bot import _redact

    msg = "for url: https://api.telegram.org/file/bot987654:XYZ-TOKEN/photos/file_0.jpg"
    out = _redact(msg)
    assert "987654:XYZ-TOKEN" not in out
    assert "https://api.telegram.org/file/bot<redacted>/photos/file_0.jpg" in out


def test_redact_redacts_http_scheme_too():
    """The pattern is scheme-agnostic (http or https) — both must be sanitized."""
    from src.telegram_bot import _redact

    msg = "for url: http://api.telegram.org/bot111:AAA/getMe"
    out = _redact(msg)
    assert "111:AAA" not in out
    assert "http://api.telegram.org/bot<redacted>/getMe" in out


def test_redact_leaves_non_telegram_strings_unchanged():
    """Strings without the Telegram bot-token URL shape pass through verbatim."""
    from src.telegram_bot import _redact

    for msg in [
        "connection timed out",
        "HTTPError: 500 Server Error",
        "https://example.com/bot/secret",  # not api.telegram.org
        "bot123:ABC",  # not embedded in a URL
        "ConnectionError: name resolution failed",
    ]:
        assert _redact(msg) == msg


def test_redact_handles_empty_string():
    from src.telegram_bot import _redact

    assert _redact("") == ""


def test_redact_handles_multiple_token_urls_in_one_message():
    """Some exceptions chain multiple URLs (e.g. retry trace); every occurrence
    must be redacted, not just the first."""
    from src.telegram_bot import _redact

    msg = (
        "first https://api.telegram.org/bot111:AAA/sendMessage failed, "
        "retry https://api.telegram.org/bot111:AAA/sendMessage also failed"
    )
    out = _redact(msg)
    assert "111:AAA" not in out
    assert out.count("bot<redacted>") == 2


def test_redact_preserves_path_after_token():
    """The redaction must stop at the token boundary — the method name and
    trailing path after the token are preserved so logs remain useful."""
    from src.telegram_bot import _redact

    msg = "for url: https://api.telegram.org/bot999:SECRET/editMessageText?chat_id=42"
    out = _redact(msg)
    assert "999:SECRET" not in out
    assert "/editMessageText?chat_id=42" in out


def test_redact_handles_token_url_in_quoted_context():
    """The regex stops at quote characters so a URL embedded in a quoted
    error message (e.g. JSON error payload) still gets its token redacted
    without swallowing the closing quote."""
    from src.telegram_bot import _redact

    msg = 'error: "https://api.telegram.org/bot42:TOK/sendMessage"'
    out = _redact(msg)
    assert "42:TOK" not in out
    assert 'bot<redacted>/sendMessage"' in out
