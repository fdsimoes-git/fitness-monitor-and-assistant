"""Claude integration for the Telegram meal-logging bot.

Two paths:

1. **Text** — `extract_meal_from_text(cfg, user_text)` calls Claude with the
   `record_meal` tool forced. The user message is the raw description; the
   tool's input is the structured row we hand to `db.insert_meal`.

2. **Photo** — `extract_meal_from_photo(cfg, image_bytes, mime_type)` calls
   Claude with two tools (`record_meal` + `extract_barcode`) and lets the
   model pick. If a packaged product's barcode is in frame, Claude returns
   the digits and the bot routes through Open Food Facts for exact data;
   otherwise Claude visually estimates the meal and returns a `record_meal`
   call. **Image bytes are never persisted** — they live on the stack only
   for the duration of the API call.

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
import logging
from typing import Any, NamedTuple

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
    """
    import anthropic  # local import — anthropic is in requirements-bot.txt only

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
