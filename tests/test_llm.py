"""Tests for src/llm.py — Anthropic credential resolution + extractors.

All Anthropic SDK calls are mocked. No real network. Tool-use blocks are
constructed as SimpleNamespace objects so they exercise the same `_first_tool_use`
attribute-access path the real SDK Message objects do.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import anthropic  # noqa: F401 — imported so we can patch it in build_anthropic_client

from src import llm
from src.config import Config


def _make_cfg(**overrides) -> Config:
    base = dict(
        garmin_email="x", garmin_password="x", garmin_token_dir=Path("/tmp/x"),
        telegram_bot_token="x", telegram_chat_id="x",
        db_path=Path("/tmp/t.db"),
        hr_resting_high_bpm=85, alert_cooldown_seconds=0,
        poll_interval_minutes=15, log_level="INFO",
        user_age=30, user_height_cm=175, user_weight_kg=75, user_sex="male",
        protein_target_g_per_kg=1.6, kcal_target=2200,
        sleep_target_hours=8, user_hrmax=0,
        anthropic_api_key="", claude_oauth_token="", claude_model="claude-sonnet-4-6",
    )
    base.update(overrides)
    return Config(**base)


# ── resolve_anthropic_auth ───────────────────────────────────────────────────


def test_resolve_auth_prefers_oauth_over_api_key():
    cfg = _make_cfg(claude_oauth_token="sk-ant-oat01-abc", anthropic_api_key="sk-ant-api03-xyz")
    auth = llm.resolve_anthropic_auth(cfg)
    assert auth.auth_token == "sk-ant-oat01-abc"
    assert auth.api_key == ""
    assert auth.has_creds is True


def test_resolve_auth_falls_back_to_api_key():
    cfg = _make_cfg(claude_oauth_token="", anthropic_api_key="sk-ant-api03-xyz")
    auth = llm.resolve_anthropic_auth(cfg)
    assert auth.auth_token == ""
    assert auth.api_key == "sk-ant-api03-xyz"


def test_resolve_auth_returns_empty_when_unset():
    cfg = _make_cfg()
    auth = llm.resolve_anthropic_auth(cfg)
    assert auth.auth_token == ""
    assert auth.api_key == ""
    assert auth.has_creds is False


# ── build_anthropic_client ──────────────────────────────────────────────────


def test_build_client_oauth_sends_beta_header():
    auth = llm.AnthropicAuth(auth_token="sk-ant-oat01-abc", api_key="")
    with patch("anthropic.Anthropic") as mock_ctor:
        mock_ctor.return_value = MagicMock()
        llm.build_anthropic_client(auth)
        mock_ctor.assert_called_once_with(
            auth_token="sk-ant-oat01-abc",
            api_key=None,
            default_headers={"anthropic-beta": llm.ANTHROPIC_OAUTH_BETA},
        )


def test_build_client_api_key_omits_beta_header():
    auth = llm.AnthropicAuth(auth_token="", api_key="sk-ant-api03-xyz")
    with patch("anthropic.Anthropic") as mock_ctor:
        mock_ctor.return_value = MagicMock()
        llm.build_anthropic_client(auth)
        # API-key path must not pass the OAuth beta header.
        kwargs = mock_ctor.call_args.kwargs
        assert kwargs.get("api_key") == "sk-ant-api03-xyz"
        assert "default_headers" not in kwargs


def test_build_client_raises_without_credentials():
    with pytest.raises(RuntimeError, match="No Anthropic credentials"):
        llm.build_anthropic_client(llm.AnthropicAuth(auth_token="", api_key=""))


def test_build_client_translates_missing_sdk_to_actionable_error():
    """If `anthropic` isn't installed, surface a runtime error pointing at requirements-bot.txt
    rather than the raw ModuleNotFoundError stack trace."""
    auth = llm.AnthropicAuth(auth_token="", api_key="sk-ant-api03-x")
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ModuleNotFoundError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import), \
         pytest.raises(RuntimeError, match="requirements-bot.txt"):
        llm.build_anthropic_client(auth)


# ── build_system_prompt ─────────────────────────────────────────────────────


def test_build_system_prompt_oauth_returns_blocks_with_claude_code_prefix():
    auth = llm.AnthropicAuth(auth_token="sk-ant-oat01-abc", api_key="")
    out = llm.build_system_prompt(auth, "Be concise.")
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": llm.CLAUDE_CODE_SYSTEM_PREFIX}
    assert out[1] == {"type": "text", "text": "Be concise."}


def test_build_system_prompt_oauth_with_empty_prompt_keeps_only_prefix():
    auth = llm.AnthropicAuth(auth_token="sk-ant-oat01-abc", api_key="")
    out = llm.build_system_prompt(auth, "")
    assert out == [{"type": "text", "text": llm.CLAUDE_CODE_SYSTEM_PREFIX}]


def test_build_system_prompt_api_key_returns_plain_string():
    auth = llm.AnthropicAuth(auth_token="", api_key="sk-ant-api03-xyz")
    out = llm.build_system_prompt(auth, "Be concise.")
    assert out == "Be concise."


# ── extract_meal_from_text ──────────────────────────────────────────────────


def _mock_response_with_tool_use(name: str, input_payload: dict, stop_reason: str = "tool_use"):
    """Build a synthetic Anthropic Message-shaped object with one tool_use block."""
    block = SimpleNamespace(type="tool_use", name=name, input=input_payload, id="toolu_x")
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def _mock_response_text_only(text: str = "ok", stop_reason: str = "end_turn"):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def test_extract_meal_from_text_returns_dict_on_tool_use():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_response_with_tool_use(
        "record_meal",
        {"description": "oat porridge", "kcal": 350, "protein_g": 12, "carbs_g": 60, "fat_g": 6},
    )
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.extract_meal_from_text(cfg, "oat porridge with banana")
    assert out == {"description": "oat porridge", "kcal": 350, "protein_g": 12, "carbs_g": 60, "fat_g": 6}
    # Confirm the call was made with the forced tool_choice.
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_meal"}
    assert kwargs["tools"] == [llm.RECORD_MEAL_TOOL]


def test_extract_meal_from_text_returns_none_when_no_tool_use():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_response_text_only()
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.extract_meal_from_text(cfg, "??")
    assert out is None


def test_extract_meal_from_text_returns_none_without_credentials():
    cfg = _make_cfg()  # no creds
    out = llm.extract_meal_from_text(cfg, "anything")
    assert out is None


# ── extract_meal_from_photo ─────────────────────────────────────────────────


def test_extract_meal_from_photo_sends_image_block_with_two_tools():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_response_with_tool_use(
        "record_meal", {"description": "plate of pasta", "kcal": 600}
    )
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.extract_meal_from_photo(cfg, b"\x89PNGfake", "image/png")

    assert out == {"kind": "meal", "meal": {"description": "plate of pasta", "kcal": 600}}
    kwargs = fake_client.messages.create.call_args.kwargs
    # Both tools must be exposed; auto-pick.
    assert kwargs["tool_choice"] == {"type": "auto"}
    assert llm.RECORD_MEAL_TOOL in kwargs["tools"]
    assert llm.EXTRACT_BARCODE_TOOL in kwargs["tools"]
    # Image block present with the right media_type.
    user_msg = kwargs["messages"][0]
    assert user_msg["role"] == "user"
    contents = user_msg["content"]
    image_blocks = [c for c in contents if c.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["media_type"] == "image/png"
    assert image_blocks[0]["source"]["type"] == "base64"
    # Base64 of b"\x89PNGfake"
    import base64
    assert image_blocks[0]["source"]["data"] == base64.b64encode(b"\x89PNGfake").decode("ascii")


def test_extract_meal_from_photo_returns_barcode_kind_when_extract_barcode_called():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_response_with_tool_use(
        "extract_barcode", {"barcode": "5449000000996"}
    )
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.extract_meal_from_photo(cfg, b"jpegbytes", "image/jpeg")
    assert out == {"kind": "barcode", "barcode": "5449000000996"}


def test_extract_meal_from_photo_returns_none_when_no_tool_use():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_response_text_only()
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.extract_meal_from_photo(cfg, b"jpegbytes", "image/jpeg")
    assert out is None


def test_extract_meal_from_photo_returns_none_without_credentials():
    out = llm.extract_meal_from_photo(_make_cfg(), b"jpegbytes", "image/jpeg")
    assert out is None


# ── chat (data-aware /ask) ──────────────────────────────────────────────────


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(tool_id: str, name: str, input_data: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_data)


def test_chat_returns_text_on_end_turn():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("Your protein has been steady at 110g/day.")],
        stop_reason="end_turn",
    )
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.chat(cfg, "How's my protein?")
    assert out == "Your protein has been steady at 110g/day."
    assert fake_client.messages.create.call_count == 1


def test_chat_returns_none_without_credentials():
    out = llm.chat(_make_cfg(), "anything")
    assert out is None


def test_chat_loops_through_tool_use():
    """tool_use → execute → tool_result → final text."""
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    tool_use_resp = SimpleNamespace(
        content=[_tool_use_block("tu_1", "get_balance", {})],
        stop_reason="tool_use",
    )
    final_resp = SimpleNamespace(
        content=[_text_block("You're 200 kcal under today.")],
        stop_reason="end_turn",
    )
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [tool_use_resp, final_resp]
    with patch("src.llm.build_anthropic_client", return_value=fake_client), \
         patch("src.llm.db.calorie_balance_for_date",
               return_value={"balance_kcal": -200, "eaten_kcal": 1500, "meal_count": 2}):
        out = llm.chat(cfg, "How's my balance today?")
    assert "200 kcal under" in out
    assert fake_client.messages.create.call_count == 2
    # Second call's messages should include both the assistant turn AND a tool_result.
    second_messages = fake_client.messages.create.call_args_list[1].kwargs["messages"]
    assert second_messages[-1]["role"] == "user"
    tool_results = second_messages[-1]["content"]
    assert tool_results[0]["type"] == "tool_result"
    assert tool_results[0]["tool_use_id"] == "tu_1"
    # The serialized result body should mention the balance number we mocked.
    assert "balance_kcal" in tool_results[0]["content"]


def test_chat_caps_iterations():
    """A model that always returns tool_use shouldn't run forever."""
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    looping = SimpleNamespace(
        content=[_tool_use_block("tu_x", "get_balance", {})],
        stop_reason="tool_use",
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = looping
    with patch("src.llm.build_anthropic_client", return_value=fake_client), \
         patch("src.llm.db.calorie_balance_for_date", return_value={}):
        out = llm.chat(cfg, "loop forever")
    assert out is None
    assert fake_client.messages.create.call_count == llm.CHAT_MAX_ITERATIONS


def test_chat_swallows_tool_exceptions_into_error_results():
    """A failing tool shouldn't crash the loop — Claude sees an error result instead."""
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    tool_use_resp = SimpleNamespace(
        content=[_tool_use_block("tu_1", "get_balance", {})],
        stop_reason="tool_use",
    )
    final_resp = SimpleNamespace(
        content=[_text_block("I had trouble reading the balance.")],
        stop_reason="end_turn",
    )
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [tool_use_resp, final_resp]
    with patch("src.llm.build_anthropic_client", return_value=fake_client), \
         patch("src.llm.db.calorie_balance_for_date", side_effect=RuntimeError("disk full")):
        out = llm.chat(cfg, "today balance?")
    assert out == "I had trouble reading the balance."
    second_call = fake_client.messages.create.call_args_list[1]
    tool_result_block = second_call.kwargs["messages"][-1]["content"][0]
    assert "disk full" in tool_result_block["content"]


# ── _execute_chat_tool dispatcher ───────────────────────────────────────────


@pytest.fixture
def tmpdb_cfg(tmp_path):
    """A real (tmp) DB so dispatcher tests exercise the actual db.py helpers."""
    from src import db as _db
    p = tmp_path / "t.db"
    _db.init_db(p)
    return _make_cfg(db_path=p, anthropic_api_key="sk-ant-api03-x")


def test_execute_chat_tool_get_balance_uses_today(tmpdb_cfg):
    out = llm._execute_chat_tool("get_balance", {}, tmpdb_cfg)
    assert "balance_kcal" in out
    assert out["meal_count"] == 0


def test_execute_chat_tool_get_recent_meals_caps_at_30(tmpdb_cfg):
    out = llm._execute_chat_tool("get_recent_meals", {"days": 9999}, tmpdb_cfg)
    assert isinstance(out, list)


def test_execute_chat_tool_unknown_returns_error(tmpdb_cfg):
    out = llm._execute_chat_tool("get_does_not_exist", {}, tmpdb_cfg)
    assert "error" in out
    assert "Unknown tool" in out["error"]


# ── write tools (validate-and-stash) ────────────────────────────────────────


def test_log_meal_tool_parks_in_pending_without_writing(tmpdb_cfg):
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool(
        "log_meal",
        {"description": "oats", "kcal": 350, "protein_g": 12},
        tmpdb_cfg, pending,
    )
    assert out["needs_confirmation"] is True
    assert out["action"] == "insert"
    assert "oats" in out["summary"]
    pid = out["pending_id"]
    assert pending[pid]["action"] == "insert"
    assert pending[pid]["meal"]["description"] == "oats"
    # Nothing in the DB yet.
    from src import db as _db
    from datetime import date
    assert _db.meals_for_date(tmpdb_cfg.db_path, date.today().isoformat()) == []


def test_log_meal_tool_rejects_missing_required_fields(tmpdb_cfg):
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool("log_meal", {"description": "oats"}, tmpdb_cfg, pending)
    assert "error" in out
    assert pending == {}


def test_edit_meal_tool_parks_edit_with_before_snapshot(tmpdb_cfg):
    from src import db as _db
    mid = _db.insert_meal(tmpdb_cfg.db_path, {"description": "salmon", "source": "manual", "kcal": 400})
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool(
        "edit_meal",
        {"meal_id": mid, "kcal": 380, "description": "salmon (corrected)"},
        tmpdb_cfg, pending,
    )
    assert out["needs_confirmation"] is True
    pid = out["pending_id"]
    assert pending[pid]["action"] == "edit"
    assert pending[pid]["meal_id"] == mid
    assert pending[pid]["fields"] == {"kcal": 380, "description": "salmon (corrected)"}
    assert pending[pid]["before"]["description"] == "salmon"


def test_edit_meal_tool_returns_error_for_missing_meal(tmpdb_cfg):
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool("edit_meal", {"meal_id": 99999, "kcal": 100}, tmpdb_cfg, pending)
    assert "error" in out
    assert pending == {}


def test_edit_meal_tool_requires_at_least_one_field(tmpdb_cfg):
    from src import db as _db
    mid = _db.insert_meal(tmpdb_cfg.db_path, {"description": "x", "source": "manual", "kcal": 100})
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool("edit_meal", {"meal_id": mid}, tmpdb_cfg, pending)
    assert "error" in out
    assert pending == {}


def test_delete_meal_tool_parks_delete_with_before_snapshot(tmpdb_cfg):
    from src import db as _db
    mid = _db.insert_meal(tmpdb_cfg.db_path, {"description": "snack", "source": "manual", "kcal": 200})
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool("delete_meal", {"meal_id": mid}, tmpdb_cfg, pending)
    assert out["needs_confirmation"] is True
    pid = out["pending_id"]
    assert pending[pid]["action"] == "delete"
    assert pending[pid]["meal_id"] == mid
    assert pending[pid]["before"]["description"] == "snack"
    # Row still present until confirm runs.
    assert _db.get_meal_by_id(tmpdb_cfg.db_path, mid) is not None


def test_delete_meal_tool_returns_error_for_missing_meal(tmpdb_cfg):
    pending: dict[str, dict] = {}
    out = llm._execute_chat_tool("delete_meal", {"meal_id": 88888}, tmpdb_cfg, pending)
    assert "error" in out
    assert pending == {}


def test_lookup_barcode_tool_returns_meal_dict_on_hit(tmpdb_cfg):
    info = {
        "name": "Acme Bar", "kcal_100g": 400, "protein_100g": 5, "carbs_100g": 60,
        "fat_100g": 14, "fiber_100g": 3, "sugars_100g": 35, "saturated_fat_100g": 8,
        "sodium_mg_100g": 80, "food_category": "Sugary snacks", "serving_size_g": 50,
    }
    with patch("src.llm.food.lookup_barcode", return_value=info):
        out = llm._execute_chat_tool("lookup_barcode", {"barcode": "1234567890123"},
                                      tmpdb_cfg, {})
    # meal_from_barcode_info default scales to serving_size_g (50g, factor 0.5).
    assert out["kcal"] == 200
    assert out["food_category"] == "Sugary snacks"
    assert out["barcode"] == "1234567890123"


def test_lookup_barcode_tool_returns_error_on_miss(tmpdb_cfg):
    with patch("src.llm.food.lookup_barcode", return_value=None):
        out = llm._execute_chat_tool("lookup_barcode", {"barcode": "9999999999"},
                                      tmpdb_cfg, {})
    assert "error" in out


# ── chat() with image input ─────────────────────────────────────────────────


def test_chat_passes_image_block_when_image_bytes_provided():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("That's a meal.")],
        stop_reason="end_turn",
    )
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.chat(cfg, "what is this?", {}, image_bytes=b"\x89PNG", mime_type="image/png")
    assert out == "That's a meal."
    user_content = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    image_blocks = [c for c in user_content if c.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["media_type"] == "image/png"
    text_blocks = [c for c in user_content if c.get("type") == "text"]
    assert text_blocks[0]["text"] == "what is this?"


def test_chat_provides_default_caption_when_image_only():
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("ok")], stop_reason="end_turn",
    )
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        llm.chat(cfg, "", {}, image_bytes=b"\x89PNG", mime_type="image/png")
    user_content = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    text_blocks = [c for c in user_content if c.get("type") == "text"]
    # Empty caption falls back to a default prompt that mentions "log it".
    assert "log" in text_blocks[0]["text"].lower()


def test_chat_prepends_history_before_the_new_user_turn():
    """When `history` is supplied, chat() prepends every entry verbatim and the
    new user message goes last."""
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("Found two — porridge and pasta.")],
        stop_reason="end_turn",
    )
    history = [
        {"role": "user", "content": "what did I eat yesterday?"},
        {"role": "assistant", "content": "Porridge at 08:00 and pasta at 19:30."},
    ]
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.chat(cfg, "what about today?", {}, history=history)
    assert out == "Found two — porridge and pasta."
    sent = fake_client.messages.create.call_args.kwargs["messages"]
    # Prior history first, then the new user message — exactly 3 entries.
    assert len(sent) == 3
    assert sent[0] == history[0]
    assert sent[1] == history[1]
    assert sent[2] == {"role": "user", "content": "what about today?"}
    # And we did NOT mutate the caller's history list.
    assert history == [
        {"role": "user", "content": "what did I eat yesterday?"},
        {"role": "assistant", "content": "Porridge at 08:00 and pasta at 19:30."},
    ]


def test_chat_with_history_and_image_sends_image_only_for_current_turn():
    """Past photo turns should already be string placeholders; chat() doesn't
    add image content blocks to historical turns."""
    cfg = _make_cfg(anthropic_api_key="sk-ant-api03-x")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("ok")], stop_reason="end_turn",
    )
    history = [
        {"role": "user", "content": "[photo] my last lunch"},
        {"role": "assistant", "content": "Logged: pasta — 600 kcal."},
    ]
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        llm.chat(cfg, "compare to today", {}, image_bytes=b"\x89PNG", mime_type="image/png", history=history)
    sent = fake_client.messages.create.call_args.kwargs["messages"]
    # Past placeholder is plain text — no image block.
    assert sent[0]["content"] == "[photo] my last lunch"
    # Current turn has the image content list.
    assert isinstance(sent[-1]["content"], list)
    assert any(c.get("type") == "image" for c in sent[-1]["content"])


def test_chat_full_log_meal_loop_parks_pending(tmpdb_cfg):
    """End-to-end through the agentic loop: model calls log_meal, tool stashes pending, model summarises."""
    from src import db as _db
    pending: dict[str, dict] = {}
    tool_use_resp = SimpleNamespace(
        content=[_tool_use_block("tu_1", "log_meal", {"description": "oats", "kcal": 350})],
        stop_reason="tool_use",
    )
    final_resp = SimpleNamespace(
        content=[_text_block("Logged: oats — 350 kcal.")],
        stop_reason="end_turn",
    )
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [tool_use_resp, final_resp]
    with patch("src.llm.build_anthropic_client", return_value=fake_client):
        out = llm.chat(tmpdb_cfg, "log oats 350 kcal", pending)
    assert out == "Logged: oats — 350 kcal."
    # Tool ran via dispatcher → pending populated → DB still empty.
    assert len(pending) == 1
    pid = next(iter(pending))
    assert pending[pid]["action"] == "insert"
    assert pending[pid]["meal"]["description"] == "oats"
    from datetime import date
    assert _db.meals_for_date(tmpdb_cfg.db_path, date.today().isoformat()) == []
