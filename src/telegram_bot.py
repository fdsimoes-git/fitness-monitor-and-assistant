"""Chat-first Telegram listener for the fitness/nutrition assistant.

Single-user, raw `requests`-based long-polling. Mirrors the outbound style
in `alerts.py` so we don't pull in a heavyweight Telegram library.

The bot is now a chat agent (mirroring asset-management's financial advisor):
plain text and photos are forwarded to `llm.chat()` with the full 12-tool
surface, split as:

- **8 read-only DB tools** — balance, meals, recent_meals, daily_summary,
  trends, activities, readiness, training_intel.
- **3 validate-and-stash write tools** — `log_meal`, `edit_meal`,
  `delete_meal`. These park a proposal in the per-process `pending` dict;
  the bot diffs `pending` before/after the chat call and surfaces a
  Confirm/Cancel inline keyboard for each new proposal. Actual
  `db.insert_meal` / `db.update_meal` / `db.delete_meal` calls only fire
  when the user taps ✅. Pattern mirrors asset-management's
  `editEntry` / `deleteEntry`.
- **1 read-only Open Food Facts tool** — `lookup_barcode`. It returns
  scaled nutrition data for a packaged product and does NOT create a
  pending write; the model typically chains it into a `log_meal` call
  afterward.

Dispatch tree
─────────────
  /help, /start                            → static help text
  /today, /balance                         → fast-path: today's calorie balance
  numeric ^\\d{8,14}$                        → fast-path: OFF lookup → propose log
  any other text (with or without caption) → llm.chat() → propose / answer
  photo (with or without caption)          → llm.chat(image_bytes=…) → propose / answer
  callback_query (confirm:UUID/cancel:UUID) → execute or drop a pending action

Photo bytes are passed to Claude in-memory and **discarded** — no SHA-256
cache, no blob columns, nothing that would let raw inputs leak into SQLite.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

import requests

from . import alerts, db, food, llm
from .config import Config

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_API = "https://api.telegram.org/bot{token}"
TELEGRAM_FILE = "https://api.telegram.org/file/bot{token}/{path}"
LONG_POLL_TIMEOUT = 30  # seconds; Telegram allows up to 50
PENDING_TTL_SECONDS = 3600
INITIAL_BACKOFF_S = 2.0
MAX_BACKOFF_S = 60.0
BARCODE_PATTERN = re.compile(r"^\d{8,14}$")
HISTORY_MAX_PAIRS = 10  # keep the last N user/assistant pairs for context continuity
THINKING_TEXT = "🔄 thinking…"

# Maps a tool name to a short, human-readable status the bot edits into the
# in-flight "thinking" message before each tool runs. Keep these snappy —
# they show up in real time while the user is waiting on Claude.
_TOOL_STATUS_LABELS: dict[str, str] = {
    "get_balance": "💰 checking today's balance",
    "get_meals": "🍽️ pulling meals",
    "get_recent_meals": "🍽️ scanning recent meals",
    "get_daily_summary": "📊 reading Garmin's daily summary",
    "get_trends": "📈 analyzing trends",
    "get_activities": "🏃 looking up activities",
    "get_readiness": "✨ computing readiness",
    "get_training_intel": "💪 crunching training metrics",
    "log_meal": "📝 preparing meal log",
    "edit_meal": "✏️ preparing edit",
    "delete_meal": "🗑️ preparing delete",
    "lookup_barcode": "🏷️ looking up barcode",
}

HELP_TEXT = (
    "*garmin-monitor — fitness & nutrition assistant*\n\n"
    "Just chat with me. I have read access to your Garmin metrics + meal log "
    "and can log, edit, or delete meals on your behalf (always with a "
    "Confirm/Cancel before any change).\n\n"
    "*Logging meals*\n"
    "• Describe it: _\"oat porridge with banana, ~350 kcal\"_\n"
    "• Send a *photo* — I estimate macros or read the barcode\n"
    "• Send a *barcode* (8–14 digits) — I scale Open Food Facts data\n\n"
    "*Editing*\n"
    "• _\"delete the snack from yesterday\"_\n"
    "• _\"change meal 12 to 250 kcal\"_\n\n"
    "*Asking*\n"
    "• _\"how's my protein this week?\"_\n"
    "• _\"should I train hard today?\"_\n"
    "• _\"what did I eat yesterday?\"_\n\n"
    "*Commands* (fast paths)\n"
    "/help — this message\n"
    "/today — today's calorie balance\n"
    "/balance — alias for /today"
)


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def run(cfg: Config) -> None:
    """Long-poll Telegram for messages until interrupted.

    `pending` is a per-process dict mapping a short UUID → a proposed write
    awaiting Confirm/Cancel. Every meal-logging path (text, photo, numeric
    barcode, and the chat write tools log_meal/edit_meal/delete_meal) parks
    its proposal here before any DB write. Stale entries are swept by
    `_sweep_pending`, called once per inbound update from `dispatch()`.
    """
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        raise RuntimeError(
            "Telegram bot needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in .env"
        )
    log.info("Bot starting (long-poll)…")
    offset: int | None = None
    backoff = INITIAL_BACKOFF_S
    pending: dict[str, dict[str, Any]] = {}
    history: list[dict[str, str]] = []
    while True:
        try:
            updates = _get_updates(cfg, offset)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    dispatch(cfg, u, pending, history)
                except Exception:  # noqa: BLE001 — never let one update kill the loop
                    log.exception("dispatch error")
            backoff = INITIAL_BACKOFF_S
        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            return
        except Exception as e:  # noqa: BLE001 — outer net catches transient failures
            log.warning("Bot loop error: %s — backing off %.1fs", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, MAX_BACKOFF_S)


def _get_updates(cfg: Config, offset: int | None) -> list[dict]:
    params: dict[str, Any] = {
        "timeout": LONG_POLL_TIMEOUT,
        "allowed_updates": '["message","callback_query"]',
    }
    if offset is not None:
        params["offset"] = offset
    url = TELEGRAM_API.format(token=cfg.telegram_bot_token) + "/getUpdates"
    r = requests.get(url, params=params, timeout=LONG_POLL_TIMEOUT + 10)
    r.raise_for_status()
    body = r.json() or {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram getUpdates returned ok=false: {body}")
    return body.get("result") or []


# ──────────────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────────────


def dispatch(
    cfg: Config,
    update: dict,
    pending: dict[str, dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> None:
    """Top-level update handler. Routes based on update kind and chat-id whitelist.

    Sweeps stale `pending` entries once per inbound update — this is the
    single chokepoint that guarantees the dict can't grow unbounded
    regardless of which path (chat, photo, barcode) created the proposal.

    `history` is the bot's per-process conversational context list (owned by
    `run()`); chat-routed handlers append to it and trim. `None` is
    accepted for backward compatibility with tests that don't care about
    history continuity.
    """
    _sweep_pending(pending)
    if history is None:
        history = []
    if "callback_query" in update:
        q = update["callback_query"]
        cb_chat = (q.get("message") or {}).get("chat", {}).get("id")
        if not _is_whitelisted(cfg, cb_chat):
            log.warning("Ignoring callback from non-whitelisted chat %s", cb_chat)
            return
        _handle_callback(cfg, q, pending)
        return

    msg = update.get("message")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    if not _is_whitelisted(cfg, chat_id):
        log.warning("Ignoring message from non-whitelisted chat %s", chat_id)
        return
    if "photo" in msg:
        _handle_photo(cfg, msg, pending, history)
    elif "text" in msg:
        _handle_text(cfg, msg, pending, history)


def _is_whitelisted(cfg: Config, chat_id: Any) -> bool:
    if chat_id is None:
        return False
    return str(chat_id) == str(cfg.telegram_chat_id)


# ──────────────────────────────────────────────────────────────────────────────
# Text flow
# ──────────────────────────────────────────────────────────────────────────────


def _handle_text(
    cfg: Config,
    msg: dict,
    pending: dict[str, dict[str, Any]],
    history: list[dict[str, str]],
) -> None:
    text = (msg.get("text") or "").strip()
    if not text:
        return

    # Fast-path commands (deterministic, no Claude call). They intentionally
    # don't append to `history` — the agent only needs to remember
    # conversational turns, not /help-style shortcuts.
    if text in ("/help", "/start"):
        alerts.send_telegram(cfg, HELP_TEXT)
        return
    if text in ("/today", "/balance"):
        # Route through db.local_today so /today and /balance share the same
        # patchable seam as the rest of the day-bucketing code (and stay
        # consistent if the helper ever changes).
        bal = db.calorie_balance_for_date(cfg.db_path, db.local_today().isoformat(), cfg=cfg)
        alerts.send_telegram(cfg, _format_balance(bal))
        return
    if BARCODE_PATTERN.match(text):
        _propose_barcode(cfg, text, pending)
        return

    # Legacy /ask + /chat support: earlier revisions exposed these as
    # explicit Q&A commands; the chat-first refactor made plain text the
    # default path. Users who still type "/ask <question>" out of habit
    # shouldn't have the literal "/ask " prefix forwarded into the model's
    # context — strip it. An empty body sends a usage hint.
    parts = text.split(maxsplit=1)
    if parts[0].lower() in ("/ask", "/chat"):
        body = parts[1].strip() if len(parts) > 1 else ""
        if not body:
            _send_plain(
                cfg,
                "Just chat with me — no /ask needed. Try a question, "
                "describe a meal, or send a photo.",
            )
            return
        text = body

    # Default: chat with the full tool surface.
    _run_chat(cfg, text, pending, history)


def _handle_photo(
    cfg: Config,
    msg: dict,
    pending: dict[str, dict[str, Any]],
    history: list[dict[str, str]],
) -> None:
    photos = msg.get("photo") or []
    if not photos:
        return
    largest = max(photos, key=lambda p: p.get("file_size") or 0)
    image_bytes, mime_type = _download_telegram_file(cfg, largest["file_id"])
    if image_bytes is None:
        _send_plain(cfg, "Couldn't download that photo from Telegram.")
        return
    caption = (msg.get("caption") or "").strip()
    _run_chat(cfg, caption, pending, history, image_bytes=image_bytes, mime_type=mime_type)
    # Reference released here; no persistence anywhere.
    del image_bytes


def _run_chat(
    cfg: Config,
    user_text: str,
    pending: dict[str, dict[str, Any]],
    history: list[dict[str, str]],
    *,
    image_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> None:
    """Dispatch to llm.chat() with live progress feedback and surface a
    Confirm/Cancel keyboard for any write proposals it parked in `pending`.

    Posts a "🔄 thinking…" status message at the start, edits it in real time
    as Claude calls each tool ("🏷️ looking up barcode 5449000000996…",
    "📝 preparing meal log…"), and either edits the final text reply into
    that same message (if the model returned text) or deletes the status
    when only proposals were created (the keyboards are the visible UX).

    Appends a (user, assistant) pair to `history` so the next turn sees the
    prior context. Past photo turns are stored as text placeholders — we
    deliberately don't replay image bytes for older turns (token-cost +
    diminishing relevance after the first reasoning pass)."""
    _send_typing(cfg)
    status_msg_id = _send_status(cfg, THINKING_TEXT)

    progress_cb = None
    if status_msg_id is not None:
        def progress_cb(tool_name: str, tool_input: dict) -> None:  # noqa: ARG001 — closure over status_msg_id
            _edit_status(cfg, status_msg_id, _format_tool_status(tool_name, tool_input))

    pending_before = set(pending.keys())
    answer = llm.chat(
        cfg, user_text, pending,
        image_bytes=image_bytes, mime_type=mime_type,
        history=history, progress_cb=progress_cb,
    )
    new_pids = [pid for pid in pending if pid not in pending_before]

    # Resolve the status message: edit-into-answer is one round-trip vs
    # delete+send (two), so we prefer it when there's a text reply.
    if answer:
        if len(answer) > 4000:
            answer = answer[:4000].rstrip() + "\n\n[…truncated]"
        if status_msg_id is not None:
            _edit_status(cfg, status_msg_id, answer)
        else:
            _send_plain(cfg, answer)
    else:
        # No text reply. If the model parked proposals, the Confirm/Cancel
        # keyboards are the visible feedback — drop the status. If nothing
        # happened at all, swap the status into a clear failure message.
        if not new_pids:
            failure = (
                "I couldn't reach Claude (check ANTHROPIC_API_KEY / "
                "CLAUDE_CODE_OAUTH_TOKEN) or didn't get a useful response."
            )
            if status_msg_id is not None:
                _edit_status(cfg, status_msg_id, failure)
            else:
                _send_plain(cfg, failure)
        elif status_msg_id is not None:
            _delete_message(cfg, status_msg_id)

    for pid in new_pids:
        _surface_pending(cfg, pid, pending[pid])

    _append_history(
        history, user_text, answer,
        has_image=image_bytes is not None,
        proposed_count=len(new_pids),
    )


def _append_history(
    history: list[dict[str, str]],
    user_text: str,
    answer: str | None,
    *,
    has_image: bool,
    proposed_count: int = 0,
) -> None:
    """Append one user/assistant pair to `history` and trim to HISTORY_MAX_PAIRS.

    Three cases for the assistant turn:
    - Real text reply → recorded verbatim.
    - No text but ≥1 proposal parked → "(proposed action — awaiting user
      confirmation)" placeholder so the model knows next turn that
      something is in flight.
    - No text and no proposals (Claude unreachable, hard error, etc.) →
      skip the entire append. A failed turn shouldn't pollute history with
      a misleading "proposed action" string.
    """
    if not answer and proposed_count == 0:
        return  # failed turn — don't write anything to history
    if has_image:
        # Replace bytes with a text placeholder so future turns don't re-send.
        # If the user supplied a caption, keep it after the marker.
        historical_user = f"[photo] {user_text}".strip() if user_text else "[photo]"
    else:
        historical_user = user_text
    historical_assistant = answer or "(proposed action — awaiting user confirmation)"
    history.append({"role": "user", "content": historical_user})
    history.append({"role": "assistant", "content": historical_assistant})
    # Trim to the last HISTORY_MAX_PAIRS pairs (each pair = 2 messages).
    excess = len(history) - HISTORY_MAX_PAIRS * 2
    if excess > 0:
        del history[:excess]


def _propose_barcode(cfg: Config, barcode: str, pending: dict[str, dict[str, Any]]) -> None:
    """Fast-path for a numeric-only message that parses as an EAN/UPC."""
    info = food.lookup_barcode(barcode)
    if info is None:
        _send_plain(cfg, f"Barcode {barcode} not found in Open Food Facts.")
        return
    meal = food.meal_from_barcode_info(info, barcode)
    pid = uuid.uuid4().hex[:10]
    pending[pid] = {"action": "insert", "meal": meal, "ts": time.time()}
    _surface_pending(cfg, pid, pending[pid])


# ──────────────────────────────────────────────────────────────────────────────
# Photo flow
# ──────────────────────────────────────────────────────────────────────────────


def _surface_pending(
    cfg: Config, pid: str, entry: dict[str, Any],
) -> None:
    """Render a Confirm/Cancel inline keyboard for a parked proposal.

    The button text and the message body adapt to `entry["action"]` so the
    user sees a different prompt for inserts vs edits vs deletes — but the
    callback_data shape (`confirm:UUID` / `cancel:UUID`) is uniform so the
    callback handler doesn't need to know about action types.
    """
    action = entry.get("action", "insert")
    confirm_label, cancel_label, body = _confirmation_text(action, entry)
    keyboard = [[
        {"text": confirm_label, "callback_data": f"confirm:{pid}"},
        {"text": cancel_label, "callback_data": f"cancel:{pid}"},
    ]]
    _send_with_keyboard(cfg, body, keyboard)


def _confirmation_text(action: str, entry: dict[str, Any]) -> tuple[str, str, str]:
    if action == "insert":
        meal = entry.get("meal") or {}
        return (
            "✅ Log it",
            "❌ Cancel",
            f"Log this meal?\n\n{_format_meal_summary(meal)}",
        )
    if action == "edit":
        before = entry.get("before") or {}
        fields = entry.get("fields") or {}
        before_summary = _format_meal_summary(before)
        change_lines = "\n".join(f"  {k}: {before.get(k, '—')} → {v}" for k, v in fields.items())
        return (
            "✏️ Apply edit",
            "❌ Cancel",
            f"Edit meal #{entry.get('meal_id')}?\n\nWas: {before_summary}\nChanges:\n{change_lines}",
        )
    if action == "delete":
        before = entry.get("before") or {}
        return (
            "🗑 Delete",
            "❌ Cancel",
            f"Delete meal #{entry.get('meal_id')}?\n\n{_format_meal_summary(before)}",
        )
    return ("✅ Confirm", "❌ Cancel", f"Confirm pending {action}?")


def _sweep_pending(pending: dict[str, dict[str, Any]], ttl: float = PENDING_TTL_SECONDS) -> None:
    now = time.time()
    stale = [k for k, v in pending.items() if now - v.get("ts", 0) > ttl]
    for k in stale:
        del pending[k]
    if stale:
        log.debug("Swept %d stale pending entries", len(stale))


# ──────────────────────────────────────────────────────────────────────────────
# Callback flow
# ──────────────────────────────────────────────────────────────────────────────


def _handle_callback(
    cfg: Config, q: dict, pending: dict[str, dict[str, Any]]
) -> None:
    data = (q.get("data") or "")
    callback_id = q.get("id")
    if not data or ":" not in data:
        _answer_callback(cfg, callback_id, "Bad callback")
        return
    action, pid = data.split(":", 1)
    msg = q.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")

    # Validate the action BEFORE touching the pending dict so a bad/unknown
    # callback doesn't silently consume a still-pending proposal.
    if action not in ("confirm", "cancel"):
        log.warning("Ignoring unknown callback action %r for pid=%s", action, pid)
        _answer_callback(cfg, callback_id, "Unknown action")
        return

    entry = pending.pop(pid, None)
    if entry is None:
        _answer_callback(cfg, callback_id, "Expired")
        if chat_id and msg_id:
            _edit_message_remove_keyboard(cfg, chat_id, msg_id, "Confirmation expired.")
        return

    if action == "confirm":
        # _apply_pending hits the DB; if SQLite is full, the schema is in a
        # weird state, or insert_meal raises for any reason, we still owe
        # Telegram an answerCallbackQuery (otherwise the spinner hangs) AND
        # the user a clear error message in place of the keyboard. Wrap.
        try:
            ack, summary = _apply_pending(cfg, entry)
        except Exception as e:  # noqa: BLE001 — never let a write failure orphan the callback
            log.exception("_apply_pending raised on %s pid=%s", entry.get("action"), pid)
            ack = "Failed"
            summary = f"⚠️ Couldn't apply that change: {e}"
        _answer_callback(cfg, callback_id, ack)
        if chat_id and msg_id:
            _edit_message_remove_keyboard(cfg, chat_id, msg_id, summary)
    else:  # cancel
        _answer_callback(cfg, callback_id, "Cancelled")
        if chat_id and msg_id:
            _edit_message_remove_keyboard(cfg, chat_id, msg_id, "❌ Cancelled.")


def _apply_pending(cfg: Config, entry: dict[str, Any]) -> tuple[str, str]:
    """Execute a confirmed pending action against the DB. Returns (callback_ack, message_text)."""
    action = entry.get("action", "insert")
    if action == "insert":
        meal = entry.get("meal") or {}
        db.insert_meal(cfg.db_path, meal)
        return "Logged", f"✅ Logged: {_format_meal_summary(meal)}"
    if action == "edit":
        meal_id = entry.get("meal_id")
        fields = entry.get("fields") or {}
        ok = db.update_meal(cfg.db_path, int(meal_id), fields)
        if not ok:
            return "Edit failed", f"⚠️ Couldn't apply edit to meal #{meal_id} (no matching row)."
        return "Updated", f"✏️ Updated meal #{meal_id}: {', '.join(fields.keys())}"
    if action == "delete":
        meal_id = entry.get("meal_id")
        before = entry.get("before") or {}
        ok = db.delete_meal(cfg.db_path, int(meal_id))
        if not ok:
            return "Delete failed", f"⚠️ Meal #{meal_id} was already gone."
        return "Deleted", f"🗑 Deleted meal #{meal_id}: {_format_meal_summary(before)}"
    return "Unknown action", f"⚠️ Don't know how to apply action '{action}'."


# ──────────────────────────────────────────────────────────────────────────────
# Telegram HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────


def _download_telegram_file(cfg: Config, file_id: str) -> tuple[bytes | None, str]:
    """Resolve a file_id to bytes via getFile + a content GET. No persistence."""
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.get(f"{api}/getFile", params={"file_id": file_id}, timeout=15)
        r.raise_for_status()
        result = (r.json() or {}).get("result") or {}
        path = result.get("file_path")
        if not path:
            return None, ""
        url = TELEGRAM_FILE.format(token=cfg.telegram_bot_token, path=path)
        rr = requests.get(url, timeout=30)
        rr.raise_for_status()
        ext = (path.rsplit(".", 1)[-1] or "jpg").lower()
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")
        return rr.content, mime
    except Exception as e:  # noqa: BLE001
        log.error("Failed to download file %s: %s", file_id, e)
        return None, ""


def _send_typing(cfg: Config) -> None:
    """Fire a one-shot 'typing…' indicator. Auto-clears after ~5s on Telegram's side."""
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        requests.post(
            f"{api}/sendChatAction",
            json={"chat_id": cfg.telegram_chat_id, "action": "typing"},
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("sendChatAction failed (non-fatal): %s", e)


def _send_status(cfg: Config, text: str) -> int | None:
    """Send a status message and return its message_id for later edits, or None on failure."""
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.post(
            f"{api}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return ((r.json() or {}).get("result") or {}).get("message_id")
    except Exception as e:  # noqa: BLE001
        log.debug("send_status failed (non-fatal): %s", e)
        return None


class _TelegramAPIError(Exception):
    """Telegram returned HTTP 200 with ok:false (application-level error)."""


def _check_telegram_ok(r: requests.Response) -> None:
    """Raise on transport errors AND on Telegram ok=false bodies.

    Telegram returns HTTP 200 with ``{ok: false, error_code, description}``
    for application-level failures (e.g. "message to edit not found"), so
    ``raise_for_status()`` alone leaves those silent.
    """
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        return
    if isinstance(body, dict) and body.get("ok") is False:
        raise _TelegramAPIError(
            f"Telegram ok=false: {body.get('error_code')} {body.get('description')}"
        )


def _retry_transient(fn, *, retries: int = 2, backoff: float = 1.0):
    """Call *fn*; retry up to *retries* times on transient network errors.

    HTTPError is only retried for 5xx and 429; other 4xx responses are
    permanent client errors and are re-raised immediately.
    """
    for attempt in range(1 + retries):
        try:
            return fn()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < retries:
                log.warning("Transient Telegram error (attempt %d/%d): %s", attempt + 1, retries + 1, e)
                time.sleep(backoff)
            else:
                raise
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status is None or (status < 500 and status != 429):
                raise
            if attempt < retries:
                log.warning("Transient Telegram HTTP %s (attempt %d/%d): %s", status, attempt + 1, retries + 1, e)
                time.sleep(backoff)
            else:
                raise


def _edit_status(cfg: Config, msg_id: int, text: str) -> None:
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        def _do():
            r = requests.post(
                f"{api}/editMessageText",
                json={"chat_id": cfg.telegram_chat_id, "message_id": msg_id, "text": text},
                timeout=5,
            )
            _check_telegram_ok(r)
        # Tight bounds: this runs on the dispatch hot path, so cap total
        # latency well below the 30s long-poll cadence.
        _retry_transient(_do, retries=1, backoff=0.5)
    except Exception as e:  # noqa: BLE001
        log.warning("edit_status failed (non-fatal): %s", e)


def _delete_message(cfg: Config, msg_id: int) -> None:
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        requests.post(
            f"{api}/deleteMessage",
            json={"chat_id": cfg.telegram_chat_id, "message_id": msg_id},
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("delete_message failed (non-fatal): %s", e)


def _format_tool_status(tool_name: str, tool_input: dict) -> str:
    """Render the per-tool status label, with light per-tool customization."""
    label = _TOOL_STATUS_LABELS.get(tool_name, f"🔧 calling {tool_name}")
    if tool_name == "lookup_barcode" and tool_input.get("barcode"):
        label = f"🏷️ looking up barcode {tool_input['barcode']}"
    elif tool_name in ("edit_meal", "delete_meal") and tool_input.get("meal_id") is not None:
        verb = "✏️ preparing edit" if tool_name == "edit_meal" else "🗑️ preparing delete"
        label = f"{verb} for meal #{tool_input['meal_id']}"
    return label + "…"


def _send_plain(cfg: Config, text: str) -> bool:
    """Send a Markdown-free message to the whitelisted chat. Used for dynamic
    output where free-text descriptions might collide with Markdown syntax."""
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.post(
            f"{api}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("send_plain failed: %s", e)
        return False


def _send_with_keyboard(cfg: Config, text: str, keyboard: list[list[dict]]) -> bool:
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        r = requests.post(
            f"{api}/sendMessage",
            json={
                "chat_id": cfg.telegram_chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("send_with_keyboard failed: %s", e)
        return False


def _answer_callback(cfg: Config, callback_id: str | None, text: str) -> None:
    if not callback_id:
        return
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        def _do():
            r = requests.post(
                f"{api}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=10,
            )
            _check_telegram_ok(r)
        _retry_transient(_do, retries=1)
    except Exception as e:  # noqa: BLE001
        log.error("answerCallbackQuery failed: %s", e)


def _edit_message_remove_keyboard(
    cfg: Config, chat_id: Any, msg_id: Any, new_text: str
) -> None:
    """Edit the message body AND drop its inline keyboard in one call.

    Telegram retains the existing reply_markup unless you explicitly send a
    new one — omitting the field is *not* the same as clearing it. So we
    pass an empty inline_keyboard array; without this the Confirm/Cancel
    buttons stayed clickable after the user already tapped one and the
    server already wrote (or skipped) the row.
    """
    api = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    try:
        def _do():
            r = requests.post(
                f"{api}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": new_text,
                    "reply_markup": {"inline_keyboard": []},
                },
                timeout=10,
            )
            _check_telegram_ok(r)
        _retry_transient(_do)
    except Exception as e:  # noqa: BLE001
        log.error("editMessageText failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────


def _format_balance(bal: dict) -> str:
    sign = "surplus" if bal["balance_kcal"] >= 0 else "deficit"
    burned = (
        f"BMR {bal['bmr_kcal']} + steps {bal['steps_burned_kcal']}"
        + (f" + activities {bal['burned_kcal']}" if bal['burned_kcal'] > 0 else "")
    )
    return (
        f"*Today {bal['date']}*\n"
        f"Eaten: {bal['eaten_kcal']:.0f} kcal · {bal['meal_count']} meal(s)\n"
        f"Burned: {bal['total_burned_kcal']} kcal ({burned})\n"
        f"*Balance: {bal['balance_kcal']:+.0f} kcal {sign}*\n"
        f"Macros: P{bal['protein_g']:.0f}g / C{bal['carbs_g']:.0f}g / F{bal['fat_g']:.0f}g"
    )


def _fmt_num(value: Any, *, fmt: str = ".0f") -> str:
    """Defensively format a number that might come from LLM tool input as a
    string ("350" instead of 350). Returns the value's string repr if
    coercion fails so the bot never crashes building a confirmation."""
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return str(value) if value is not None else "—"


def _format_meal_summary(meal: dict) -> str:
    desc = meal.get("description") or "—"
    parts = [desc]
    kcal = meal.get("kcal")
    if kcal is not None:
        parts.append(f"{_fmt_num(kcal)} kcal")
    macros = []
    for label, key in (("P", "protein_g"), ("C", "carbs_g"), ("F", "fat_g")):
        v = meal.get(key)
        if v is not None:
            macros.append(f"{_fmt_num(v)}g {label}")
    if macros:
        parts.append(" / ".join(macros))
    return " · ".join(parts)
