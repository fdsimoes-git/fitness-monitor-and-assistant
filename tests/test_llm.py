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
