"""Claude integration for the Telegram meal-logging bot.

Three paths:

1. **Text → meal** — `extract_meal_from_text(cfg, user_text)` calls Claude
   with the `record_meal` tool forced. The user message is the raw
   description; the tool's input is the structured row we hand to
   `db.insert_meal`.

2. **Photo → meal or barcode** — `extract_meal_from_photo(...)` calls
   Claude with two tools (`record_meal` + `extract_barcode`) and lets the
   model pick. If a packaged product's barcode is in frame, Claude returns
   the digits and the bot routes through Open Food Facts; otherwise Claude
   visually estimates the meal. **Image bytes are never persisted** — they
   live on the stack only for the duration of the API call.

3. **Free-form chat** — `chat(cfg, user_text, pending, image_bytes=…)` runs
   an agentic loop with twelve tools (`CHAT_TOOLS`): eight read-only DB
   tools (balance, meals, daily summary, trends, activities, readiness,
   training intel) plus four "validate-and-stash" write tools (`log_meal`,
   `edit_meal`, `delete_meal`, `lookup_barcode`). Write tools never touch
   the DB; they park a proposal in the caller's `pending` dict and the
   Telegram bot surfaces a Confirm/Cancel keyboard before any actual write.
   This is the default text + photo path. Claude can chain tool calls;
   we cap iterations at `CHAT_MAX_ITERATIONS` to stop runaways.

Credential resolution mirrors the asset-management pattern (`server.js`
lines 241–302): if `CLAUDE_CODE_OAUTH_TOKEN` is set, calls go via the OAuth
route (Bearer token + `anthropic-beta: oauth-2025-04-20` header + a system
prefix identifying the request as Claude Code) and bill the user's Claude
Code subscription. Otherwise the standard `ANTHROPIC_API_KEY` route is used.

Omitting the "You are Claude Code" system prefix on the OAuth route makes
non-Haiku models fail with a misleading "credit balance / quota exceeded"
error rather than a proper auth failure — see `CLAUDE.md` gotchas.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any, NamedTuple

from . import db, food
from .config import Config

log = logging.getLogger(__name__)

ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_MODEL = "claude-sonnet-4-6"
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


class AnthropicAuth(NamedTuple):
    """Resolved Anthropic credentials. Exactly one field is non-empty (or both empty if no creds)."""

    auth_token: str  # Claude Code OAuth token (sk-ant-oat01-...)
    api_key: str     # Standard Anthropic API key (sk-ant-api03-...)

    @property
    def has_creds(self) -> bool:
        return bool(self.auth_token or self.api_key)


def resolve_anthropic_auth(cfg: Config) -> AnthropicAuth:
    """Pick credentials from env. OAuth wins if both are set."""
    token = (cfg.claude_oauth_token or "").strip()
    api_key = (cfg.anthropic_api_key or "").strip()
    if token:
        return AnthropicAuth(auth_token=token, api_key="")
    if api_key:
        return AnthropicAuth(auth_token="", api_key=api_key)
    return AnthropicAuth(auth_token="", api_key="")


def build_anthropic_client(auth: AnthropicAuth):
    """Construct an Anthropic SDK client. Imports the SDK lazily so the rest of
    the project doesn't pay the import cost when the bot isn't installed.

    Raises a clear `RuntimeError` (rather than the raw `ModuleNotFoundError`)
    when the SDK isn't on the path — directs the operator to the
    `requirements-bot.txt` install step.
    """
    try:
        import anthropic  # in requirements-bot.txt only
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "anthropic SDK is not installed. The Telegram bot and `log-meal-ai` "
            "subcommand require it. Install with:\n"
            "    .venv/bin/pip install -r requirements-bot.txt"
        ) from e

    if auth.auth_token:
        return anthropic.Anthropic(
            auth_token=auth.auth_token,
            api_key=None,
            default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
        )
    if auth.api_key:
        return anthropic.Anthropic(api_key=auth.api_key)
    raise RuntimeError(
        "No Anthropic credentials found — set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in .env"
    )


def build_system_prompt(auth: AnthropicAuth, prompt: str):
    """Return a system value compatible with the chosen auth route.

    On OAuth: a list of text blocks beginning with the Claude Code identifier
    (required to avoid the misleading credit-balance error on Sonnet/Opus).
    On API key: the prompt as a plain string.
    """
    if auth.auth_token:
        blocks = [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}]
        if prompt:
            blocks.append({"type": "text", "text": prompt})
        return blocks
    return prompt


# ──────────────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────────────

# Mirror of the columns accepted by `db.insert_meal`. `description` and
# `kcal` are required; everything else is optional. `food_category` is
# free-text but the prompt nudges Claude toward Open Food Facts'
# `pnns_groups_2` style ("Vegetables", "Fish, Meat, Eggs", "Sugary snacks", …).
RECORD_MEAL_TOOL: dict[str, Any] = {
    "name": "record_meal",
    "description": (
        "Save one meal to the local SQLite DB. Round numeric fields to integers when "
        "you can; leave fields out when you genuinely don't know rather than guessing."
    ),
    "input_schema": {
        "type": "object",
        "required": ["description", "kcal"],
        "properties": {
            "description": {"type": "string"},
            "kcal": {"type": "number"},
            "protein_g": {"type": "number"},
            "carbs_g": {"type": "number"},
            "fat_g": {"type": "number"},
            "fiber_g": {"type": "number"},
            "sugars_g": {"type": "number"},
            "saturated_fat_g": {"type": "number"},
            "sodium_mg": {"type": "number"},
            "food_category": {
                "type": "string",
                "description": (
                    "Open Food Facts pnns_groups_2 style category — e.g. 'Vegetables', "
                    "'Fish, Meat, Eggs', 'Sugary snacks', 'Cereals', 'Milk and dairy products'."
                ),
            },
            "meal_time": {
                "type": "string",
                "description": "ISO-8601 timestamp; defaults to now if omitted.",
            },
        },
    },
}

EXTRACT_BARCODE_TOOL: dict[str, Any] = {
    "name": "extract_barcode",
    "description": (
        "Use ONLY when a packaged-product barcode is clearly visible in the image. "
        "Return just the digits — the host application looks the product up in Open Food Facts."
    ),
    "input_schema": {
        "type": "object",
        "required": ["barcode"],
        "properties": {
            "barcode": {
                "type": "string",
                "pattern": r"^\d{8,14}$",
                "description": "EAN-8/12/13 or UPC-A digits, no spaces or dashes.",
            },
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Extractors
# ──────────────────────────────────────────────────────────────────────────────

_TEXT_SYSTEM_PROMPT = (
    "You convert free-form meal descriptions into structured nutrition rows. "
    "Always respond by calling the record_meal tool. Round numbers to integers. "
    "If a macro is unknown, omit the field rather than guessing. Use Open Food "
    "Facts pnns_groups_2 style food categories ('Vegetables', 'Fish, Meat, Eggs', "
    "'Sugary snacks', 'Cereals', etc.)."
)

_PHOTO_SYSTEM_PROMPT = (
    "You log meals from photographs. Two paths:\n"
    "  • If a packaged product's barcode is clearly visible, call extract_barcode "
    "with the digits — the host application will fetch exact nutrition from Open "
    "Food Facts. Do NOT call record_meal in this case.\n"
    "  • Otherwise, estimate the visible portion and call record_meal. Note any "
    "uncertainty in the description ('approx. 1 cup', 'small bowl', etc.). Round "
    "numbers to integers; omit unknown macros rather than guessing."
)

_PHOTO_USER_PROMPT = (
    "If a product barcode is clearly visible, call extract_barcode with the digits. "
    "Otherwise estimate the visible portion and call record_meal."
)


def _first_tool_use(response, name: str | None = None):
    """Return the first tool_use content block, optionally filtered by tool name."""
    for block in getattr(response, "content", []) or []:
        # SDK content blocks expose a `.type` attribute or a `type` key on dicts.
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype != "tool_use":
            continue
        bname = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
        if name is None or bname == name:
            return block
    return None


def _block_input(block) -> dict:
    """Extract the `input` payload from a tool_use block (object or dict)."""
    if hasattr(block, "input"):
        return dict(block.input or {})
    return dict((block or {}).get("input") or {})


def _block_to_dict(block) -> dict:
    """Normalize a content block (SDK pydantic, plain dict, or test
    SimpleNamespace) to a JSON-serializable dict suitable for sending back to
    the API on the next loop iteration. The Anthropic SDK *does* accept its
    own block objects on input, but normalizing keeps the wire format stable
    and avoids any reliance on SDK serializer internals.
    """
    if isinstance(block, dict):
        return dict(block)
    # Real SDK objects expose `model_dump`.
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump()
        except Exception:  # noqa: BLE001 — fall through to manual extraction
            pass
    # Manual fallback (covers SimpleNamespace and any future block shape).
    btype = getattr(block, "type", None)
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "input": getattr(block, "input", {}) or {},
        }
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    # Last-resort: copy public attrs into a dict.
    return {"type": btype, **{
        k: v for k, v in vars(block).items() if not k.startswith("_")
    }}


def extract_meal_from_text(cfg: Config, user_text: str) -> dict | None:
    """Run a meal description through Claude and return the structured meal dict.

    Returns the `record_meal` tool input as a plain dict, or `None` if Claude
    didn't call the tool (rare with `tool_choice` forcing it, but handled for
    safety) or if no credentials are configured.
    """
    auth = resolve_anthropic_auth(cfg)
    if not auth.has_creds:
        log.warning("No Anthropic credentials — set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        return None

    client = build_anthropic_client(auth)
    response = client.messages.create(
        model=cfg.claude_model or DEFAULT_MODEL,
        max_tokens=1024,
        system=build_system_prompt(auth, _TEXT_SYSTEM_PROMPT),
        messages=[{"role": "user", "content": user_text}],
        tools=[RECORD_MEAL_TOOL],
        tool_choice={"type": "tool", "name": "record_meal"},
    )

    block = _first_tool_use(response, name="record_meal")
    if block is None:
        log.warning("Claude returned no record_meal tool_use (stop_reason=%s)",
                    getattr(response, "stop_reason", "?"))
        return None
    return _block_input(block)


def extract_meal_from_photo(
    cfg: Config, image_bytes: bytes, mime_type: str
) -> dict | None:
    """Run a meal photo through Claude and return either a meal dict or a barcode.

    Returns one of:
      - `{"kind": "meal", "meal": {...record_meal input...}}`
      - `{"kind": "barcode", "barcode": "1234567890123"}`
      - `None` if no creds, no tool call, or an unhandled stop reason.

    `image_bytes` is base64-encoded for the API call and then released — nothing
    is written to disk or DB.
    """
    auth = resolve_anthropic_auth(cfg)
    if not auth.has_creds:
        log.warning("No Anthropic credentials — set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        return None

    client = build_anthropic_client(auth)
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    response = client.messages.create(
        model=cfg.claude_model or DEFAULT_MODEL,
        max_tokens=1024,
        system=build_system_prompt(auth, _PHOTO_SYSTEM_PROMPT),
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type or "image/jpeg",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": _PHOTO_USER_PROMPT},
            ],
        }],
        tools=[RECORD_MEAL_TOOL, EXTRACT_BARCODE_TOOL],
        tool_choice={"type": "auto"},
    )
    # explicit drop of the bytes we encoded
    del image_b64

    barcode_block = _first_tool_use(response, name="extract_barcode")
    if barcode_block is not None:
        digits = (_block_input(barcode_block).get("barcode") or "").strip()
        if digits:
            return {"kind": "barcode", "barcode": digits}

    meal_block = _first_tool_use(response, name="record_meal")
    if meal_block is not None:
        return {"kind": "meal", "meal": _block_input(meal_block)}

    log.warning(
        "Claude returned no recognisable tool_use from photo (stop_reason=%s)",
        getattr(response, "stop_reason", "?"),
    )
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Chat with read-only DB tools (data-aware /ask flow)
# ──────────────────────────────────────────────────────────────────────────────

CHAT_MAX_ITERATIONS = 6  # cap on tool-use loops per chat() invocation


# Tools the model can call. Eight are read-only, four are "validate-and-stash"
# write tools that mirror asset-management's two-phase pattern: the tool
# parks a proposal in the per-process `pending` dict and returns
# {pending_id, needs_confirmation, summary} — actual DB writes happen only
# when the user taps ✅ in Telegram (handled by telegram_bot._handle_callback).
CHAT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_balance",
        "description": (
            "Calorie balance for one date: eaten kcal, burned breakdown "
            "(BMR + step kcal + activity kcal), net balance, and macro totals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO YYYY-MM-DD; defaults to today."},
            },
        },
    },
    {
        "name": "get_meals",
        "description": "Every meal logged for one date — time, description, kcal, macros, food category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO YYYY-MM-DD; defaults to today."},
            },
        },
    },
    {
        "name": "get_recent_meals",
        "description": (
            "All meals logged across the last N days, oldest first. "
            "Use for eating-pattern questions (frequency, timing, repeated foods)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days; capped at 30. Default 7."},
            },
        },
    },
    {
        "name": "get_daily_summary",
        "description": (
            "Garmin's per-day metrics for one date: resting HR, max HR, avg HR, steps, "
            "sleep seconds, average stress, body battery, overnight HRV."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO YYYY-MM-DD; defaults to today."},
            },
        },
    },
    {
        "name": "get_trends",
        "description": (
            "Daily metrics (date, resting_hr, hrv_overnight) for the last N days. "
            "Use for trend questions ('is my RHR rising?', 'how's HRV been?')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days; capped at 90. Default 14."},
            },
        },
    },
    {
        "name": "get_activities",
        "description": "Recent Garmin activities — type, duration, distance, avg HR, calories, training effect.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days; capped at 90. Default 14."},
            },
        },
    },
    {
        "name": "get_readiness",
        "description": (
            "Composite readiness score (0-100) plus the four component deltas vs the "
            "user's 7-day baseline (HRV, RHR, sleep, body battery)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO YYYY-MM-DD; defaults to today."},
            },
        },
    },
    {
        "name": "get_training_intel",
        "description": (
            "Acute-to-chronic workload ratio (Gabbett bands), Foster training monotony, "
            "Z2 minutes for the trailing week, and 7-day sleep debt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO YYYY-MM-DD; defaults to today."},
            },
        },
    },
    # ── Write tools (validate-and-stash; user confirms in Telegram) ──
    {
        "name": "log_meal",
        "description": (
            "PROPOSE inserting a meal into the user's log. Does NOT write immediately — the bot "
            "will surface a Confirm/Cancel inline keyboard; only on ✅ does the row land in SQLite. "
            "Use after estimating macros from a description, scaling barcode data, or extracting "
            "from an image. Required: description and kcal."
        ),
        "input_schema": {
            "type": "object",
            "required": ["description", "kcal"],
            "properties": {
                "description": {"type": "string"},
                "kcal": {"type": "number"},
                "protein_g": {"type": "number"},
                "carbs_g": {"type": "number"},
                "fat_g": {"type": "number"},
                "fiber_g": {"type": "number"},
                "sugars_g": {"type": "number"},
                "saturated_fat_g": {"type": "number"},
                "sodium_mg": {"type": "number"},
                "food_category": {
                    "type": "string",
                    "description": "OFF pnns_groups_2 style: 'Vegetables', 'Sugary snacks', 'Cereals', etc.",
                },
                "meal_time": {"type": "string", "description": "ISO timestamp; defaults to now."},
            },
        },
    },
    {
        "name": "edit_meal",
        "description": (
            "PROPOSE editing an existing meal by id. Pass meal_id (from get_meals / get_recent_meals) "
            "and only the fields you want to change. The bot surfaces Confirm/Cancel before the "
            "update runs."
        ),
        "input_schema": {
            "type": "object",
            "required": ["meal_id"],
            "properties": {
                "meal_id": {"type": "integer"},
                "description": {"type": "string"},
                "kcal": {"type": "number"},
                "protein_g": {"type": "number"},
                "carbs_g": {"type": "number"},
                "fat_g": {"type": "number"},
                "fiber_g": {"type": "number"},
                "sugars_g": {"type": "number"},
                "saturated_fat_g": {"type": "number"},
                "sodium_mg": {"type": "number"},
                "food_category": {"type": "string"},
                "meal_time": {"type": "string"},
            },
        },
    },
    {
        "name": "delete_meal",
        "description": (
            "PROPOSE deleting a meal by id. The bot surfaces Confirm/Cancel before the delete runs."
        ),
        "input_schema": {
            "type": "object",
            "required": ["meal_id"],
            "properties": {"meal_id": {"type": "integer"}},
        },
    },
    {
        "name": "lookup_barcode",
        "description": (
            "Look up nutrition for a packaged product by EAN/UPC barcode in Open Food Facts. "
            "Returns the nutrition payload as a meal-shaped dict (kcal, macros, fiber, etc.) "
            "scaled to the requested grams (or the API's serving size, or 100g). "
            "Use the result's fields to construct a log_meal call, OR call log_meal directly "
            "after this — the response is meant to be consumed, not displayed verbatim."
        ),
        "input_schema": {
            "type": "object",
            "required": ["barcode"],
            "properties": {
                "barcode": {"type": "string", "pattern": r"^\d{8,14}$"},
                "grams": {
                    "type": "number",
                    "description": "Serving size to scale to. Optional; defaults to OFF's serving_size_g or 100g.",
                },
            },
        },
    },
]


# Allowed keys for the `log_meal` tool's `meal` payload. Mirrors the
# `RECORD_MEAL_TOOL` input_schema — anything outside this set the model
# might send (e.g. `id`, `logged_at`, `raw_json`) is silently dropped.
_LOG_MEAL_WRITE_FIELDS = frozenset({
    "description", "kcal", "protein_g", "carbs_g", "fat_g",
    "fiber_g", "sugars_g", "saturated_fat_g", "sodium_mg",
    "food_category", "meal_time",
})


def _execute_chat_tool(
    name: str,
    input_data: dict,
    cfg: Config,
    pending: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Dispatcher mapping tool name → existing db.py helper or write proposal.

    Read-only tools return JSON-serializable data straight from the DB.
    Write tools (log_meal, edit_meal, delete_meal) park a proposal in
    `pending` and return {pending_id, needs_confirmation, summary} so Claude
    can describe what's about to happen — the actual DB write only fires
    when the user taps ✅ in Telegram.

    Returns `{"error": "..."}` on recognised failures so the model can
    adapt instead of crashing the loop.
    """
    if pending is None:
        pending = {}
    # Route every "what local day is it?" lookup through `db.local_today` so
    # the chat tools share the codebase-wide patchable seam (mocked in the
    # _local_today regression tests in test_nutrition.py).
    today_d = db.local_today()
    today = today_d.isoformat()
    target = (input_data.get("date") or today)

    # ── Read tools ────────────────────────────────────────────────────────
    if name == "get_balance":
        return db.calorie_balance_for_date(cfg.db_path, target, cfg=cfg)
    if name == "get_meals":
        return db.meals_for_date(cfg.db_path, target)
    if name == "get_recent_meals":
        days = max(1, min(int(input_data.get("days") or 7), 30))
        out: list[dict] = []
        for offset in range(days - 1, -1, -1):
            d_iso = (today_d - timedelta(days=offset)).isoformat()
            out.extend(db.meals_for_date(cfg.db_path, d_iso))
        return out
    if name == "get_daily_summary":
        return db.daily_summary_for(cfg.db_path, target)
    if name == "get_trends":
        days = max(1, min(int(input_data.get("days") or 14), 90))
        return db.recent_daily_metrics(cfg.db_path, days)
    if name == "get_activities":
        days = max(1, min(int(input_data.get("days") or 14), 90))
        return db.recent_activities(cfg.db_path, days=days)
    if name == "get_readiness":
        return db.composite_readiness(cfg.db_path, target, cfg)
    if name == "get_training_intel":
        return {
            "acwr": db.acwr(cfg.db_path, target),
            "monotony": db.training_monotony(cfg.db_path, target),
            "z2": db.z2_minutes_for_week(cfg.db_path, target, cfg),
            "sleep_debt": db.sleep_debt(cfg.db_path, target, cfg),
        }
    if name == "lookup_barcode":
        info = food.lookup_barcode(str(input_data.get("barcode") or "").strip())
        if info is None:
            return {"error": "Barcode not found in Open Food Facts."}
        grams = input_data.get("grams")
        return food.meal_from_barcode_info(info, str(input_data["barcode"]), grams=grams)

    # ── Write tools (validate-and-stash) ──────────────────────────────────
    if name == "log_meal":
        # Whitelist keys to the RECORD_MEAL_TOOL schema. Drops anything the
        # model might send by accident (`id`, `logged_at`, `raw_json`, etc.)
        # and keeps the proposal payload faithful to the contract — defense
        # in depth on top of db.insert_meal's own column-by-column build.
        meal = {k: v for k, v in input_data.items() if k in _LOG_MEAL_WRITE_FIELDS}
        if "description" not in meal or "kcal" not in meal:
            return {"error": "log_meal requires description and kcal"}
        meal["source"] = "ai"  # always tag chat-driven logs; never trust input
        return _stash_pending(pending, action="insert", meal=meal)

    if name == "edit_meal":
        try:
            meal_id = int(input_data.get("meal_id"))
        except (TypeError, ValueError):
            return {"error": "edit_meal requires an integer meal_id"}
        before = db.get_meal_by_id(cfg.db_path, meal_id)
        if before is None:
            return {"error": f"Meal #{meal_id} not found."}
        # Filter to columns the DB actually allows mutating BEFORE stashing.
        # Without this, `db.update_meal` silently drops anything outside
        # `EDITABLE_MEAL_COLUMNS` and the proposal lies about what it'll do —
        # then update_meal returns False and the user sees a misleading
        # "no matching row" failure.
        fields = {
            k: v for k, v in input_data.items()
            if k != "meal_id" and k in db.EDITABLE_MEAL_COLUMNS
        }
        if not fields:
            return {
                "error": (
                    "edit_meal requires at least one editable field. Allowed: "
                    + ", ".join(sorted(db.EDITABLE_MEAL_COLUMNS))
                )
            }
        return _stash_pending(
            pending, action="edit", meal_id=meal_id, fields=fields, before=before,
        )

    if name == "delete_meal":
        try:
            meal_id = int(input_data.get("meal_id"))
        except (TypeError, ValueError):
            return {"error": "delete_meal requires an integer meal_id"}
        before = db.get_meal_by_id(cfg.db_path, meal_id)
        if before is None:
            return {"error": f"Meal #{meal_id} not found."}
        return _stash_pending(pending, action="delete", meal_id=meal_id, before=before)

    return {"error": f"Unknown tool: {name}"}


def _stash_pending(
    pending: dict[str, dict[str, Any]], *, action: str, **payload: Any,
) -> dict[str, Any]:
    """Park a proposal in `pending` and return the contract Claude expects:
    a pending_id, a flag the model can describe, and a short summary."""
    pid = uuid.uuid4().hex[:10]
    entry = {"action": action, "ts": time.time(), **payload}
    pending[pid] = entry
    return {
        "pending_id": pid,
        "needs_confirmation": True,
        "action": action,
        "summary": _summarize_pending(entry),
    }


def _summarize_pending(entry: dict) -> str:
    action = entry.get("action")
    if action == "insert":
        meal = entry.get("meal") or {}
        kcal = meal.get("kcal")
        return f"Log: {meal.get('description', '?')} — {int(kcal) if kcal is not None else '—'} kcal"
    if action == "edit":
        fields = entry.get("fields") or {}
        keys = ", ".join(fields.keys()) or "(no fields)"
        return f"Edit meal #{entry.get('meal_id')}: change {keys}"
    if action == "delete":
        before = entry.get("before") or {}
        return f"Delete meal #{entry.get('meal_id')}: {before.get('description', '?')}"
    return f"Pending {action}"


_CHAT_SYSTEM_TEMPLATE = (
    "You are the user's personal fitness and nutrition assistant for the "
    "garmin-monitor app. You have direct access to their local SQLite DB via "
    "the provided tools — both read tools (balance, meals, trends, readiness, "
    "training intel) and write tools (log_meal, edit_meal, delete_meal). You "
    "are also given a lookup_barcode tool that hits Open Food Facts.\n\n"
    "Today is {today}.\n\n"
    "Rules:\n"
    "• Cite specific numbers from tool results — never invent values.\n"
    "• When the user describes a meal (text or photo) call log_meal to "
    "propose it. Do NOT explain that you need a confirmation: the bot will "
    "show a Confirm/Cancel keyboard automatically when log_meal returns. "
    "Just describe what you proposed in past tense (\"Logged: …\").\n"
    "• If a packaged-product barcode is visible in a photo or message, call "
    "lookup_barcode first, then log_meal with the result's fields.\n"
    "• To edit or remove a meal, first call get_meals or get_recent_meals to "
    "find the meal_id, then call edit_meal or delete_meal.\n"
    "• Convert relative time references (\"yesterday\", \"this week\") to ISO "
    "YYYY-MM-DD dates yourself before calling any tool — the tools accept ISO "
    "dates only.\n"
    "• Replies render in a Telegram chat. Keep them concise (1–3 sentences "
    "unless the user asks for detail). Plain text — no Markdown.\n"
    "• You may chain multiple tool calls in one turn."
)


def chat(
    cfg: Config,
    user_text: str,
    pending: dict[str, dict[str, Any]] | None = None,
    *,
    image_bytes: bytes | None = None,
    mime_type: str | None = None,
    history: list[dict] | None = None,
    progress_cb: "Any | None" = None,
) -> str | None:
    """Run an agentic conversation turn against `CHAT_TOOLS` and return the
    final text answer.

    `pending` is the bot's per-process Map of pending write proposals. Write
    tools (log_meal/edit_meal/delete_meal) mutate it in place; the caller
    diffs before/after to know which proposals to surface as Confirm/Cancel
    keyboards.

    `image_bytes` + `mime_type` make the model see the image as part of the
    initial user turn — passed through to Claude as a base64 image content
    block. The bytes are never persisted (the bot drops them after this
    call returns).

    `history` is the conversational context from prior turns — a list of
    `{role, content}` dicts owned by the caller (the bot keeps a per-process
    list capped at HISTORY_MAX_PAIRS user/assistant exchanges). It is
    prepended to the messages list verbatim; this function does NOT mutate
    it. Past photo turns should be stored as text placeholders by the
    caller — we deliberately don't re-send image bytes for older turns.

    `progress_cb`, when supplied, is called as `progress_cb(tool_name,
    tool_input)` immediately before each tool is executed. Used by the
    Telegram bot to surface "🏷️ looking up barcode…" style status updates
    while the agent is mid-loop. Failures inside the callback are caught
    so a broken UI hook never breaks the chat turn.

    Returns `None` if no credentials, no text in the final reply, or the
    loop exceeds `CHAT_MAX_ITERATIONS`.
    """
    auth = resolve_anthropic_auth(cfg)
    if not auth.has_creds:
        log.warning("No Anthropic credentials — set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        return None
    if pending is None:
        pending = {}

    client = build_anthropic_client(auth)
    system = build_system_prompt(
        auth, _CHAT_SYSTEM_TEMPLATE.format(today=date.today().isoformat())
    )

    if image_bytes is not None:
        user_content: Any = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type or "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": user_text or "What's in this photo? Log it if it's a meal."},
        ]
    else:
        user_content = user_text

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": user_content})

    for _ in range(CHAT_MAX_ITERATIONS):
        response = client.messages.create(
            model=cfg.claude_model or DEFAULT_MODEL,
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=CHAT_TOOLS,
        )

        if getattr(response, "stop_reason", None) == "tool_use":
            # Normalize SDK content blocks to plain dicts before re-sending.
            # The SDK accepts its own pydantic blocks on input, but plain
            # dicts are more robust across SDK versions and let our test
            # fixtures (SimpleNamespace) round-trip through the same path.
            messages.append({
                "role": "assistant",
                "content": [_block_to_dict(b) for b in response.content],
            })
            tool_results: list[dict] = []
            for block in response.content:
                btype = getattr(block, "type", None) or (
                    block.get("type") if isinstance(block, dict) else None
                )
                if btype != "tool_use":
                    continue
                name = getattr(block, "name", None) or (
                    block.get("name") if isinstance(block, dict) else None
                )
                input_data = _block_input(block)
                if progress_cb is not None:
                    try:
                        progress_cb(name, input_data)
                    except Exception:  # noqa: BLE001 — UI hook must never break the loop
                        log.exception("progress_cb raised — continuing")
                try:
                    result = _execute_chat_tool(name, input_data, cfg, pending)
                except Exception as e:  # noqa: BLE001 — surface as tool error
                    log.exception("Chat tool %s raised", name)
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": getattr(block, "id", None) or (
                        block.get("id") if isinstance(block, dict) else None
                    ),
                    "content": json.dumps(result, default=str),
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        texts: list[str] = []
        for block in response.content:
            btype = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if btype != "text":
                continue
            t = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else None
            )
            if t:
                texts.append(t)
        return "\n".join(texts).strip() or None

    log.warning("chat() exceeded %d tool-use iterations", CHAT_MAX_ITERATIONS)
    return None
